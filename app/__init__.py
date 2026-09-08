"""Application package initialization and Telegram download policy."""

import os
from urllib.parse import parse_qs, unquote, urlparse


# Telegram files must be downloaded from their actual document DC. Never force
# a different DC here; doing so previously caused FileMigrate/DcIdInvalid errors.
_requested_dc = os.getenv("TELEGRAM_DOWNLOAD_DC", "auto").strip().lower()
if _requested_dc not in {"", "auto"}:
    print(
        "Telegram download DC override is disabled for file downloads; "
        f"requested={_requested_dc!r}, using auto/document DC instead.",
        flush=True,
    )
    os.environ["TELEGRAM_DOWNLOAD_DC"] = "auto"


# The downloader imports ConnectionTcpAbridged directly. Keep transport and
# route selection centralized here so the existing bot.py remains compatible.
# Telethon 1.40.0 supports classic MTProxy through tcpmtproxy.py, including
# hex/base64 secrets. Modern Fake-TLS (ee...) secrets are intentionally rejected
# because they need a different protocol implementation than this pinned
# Telethon version.
_proxy_url = os.getenv("TELEGRAM_DOWNLOAD_PROXY_URL", "").strip()
_mtproxy_config = None


def _parse_mtproxy_url(value: str):
    """Parse common MTProto proxy links without logging the secret."""
    if not value:
        return None

    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if scheme in {"tg", "https", "http"} and parsed.query:
        query = parse_qs(parsed.query)
        if "server" in query and "port" in query and "secret" in query:
            host = query["server"][0].strip()
            port = int(query["port"][0])
            secret = unquote(query["secret"][0]).strip()
            if host and 1 <= port <= 65535 and secret:
                return host, port, secret

    if scheme in {"mtproto", "mtproxy"}:
        host = parsed.hostname
        port = parsed.port
        secret = ""
        if parsed.path and parsed.path != "/":
            secret = unquote(parsed.path.lstrip("/"))
        if not secret:
            query = parse_qs(parsed.query)
            secret = unquote(query.get("secret", [""])[0]).strip()
        if host and port and secret:
            return host, port, secret

    return None


_mtproxy_config = _parse_mtproxy_url(_proxy_url)
if _mtproxy_config is not None:
    _mtproxy_host, _mtproxy_port, _mtproxy_secret = _mtproxy_config
    if _mtproxy_secret.lower().startswith("ee"):
        raise ValueError(
            "This Telethon 1.40.0 build does not support modern Fake-TLS "
            "MTProxy secrets starting with 'ee'. Use a classic MTProxy secret "
            "for this benchmark."
        )

    # bot.py currently validates TELEGRAM_DOWNLOAD_PROXY_URL as a SOCKS/HTTP
    # URL. Convert only the visible proxy configuration to a harmless HTTP-shaped
    # value so its existing validation/logging stays enabled. The actual socket
    # class below ignores that value and uses the real MTProxy secret.
    os.environ["TELEGRAM_DOWNLOAD_PROXY_URL"] = (
        f"http://{_mtproxy_host}:{_mtproxy_port}"
    )


_transport = os.getenv("TELEGRAM_DOWNLOAD_TRANSPORT", "full").strip().lower()
if _mtproxy_config is not None:
    _transport = "mtproxy"
elif _transport not in {"full", "abridged"}:
    raise ValueError(
        "TELEGRAM_DOWNLOAD_TRANSPORT must be 'full' or 'abridged'"
    )


if _mtproxy_config is not None:
    import telethon.network.connection.tcpabridged as _tcpabridged
    from telethon.network.connection.tcpmtproxy import (
        ConnectionTcpMTProxyAbridged,
        ConnectionTcpMTProxyRandomizedIntermediate,
    )

    _mtproxy_host, _mtproxy_port, _mtproxy_secret = _mtproxy_config
    _mtproxy_base = (
        ConnectionTcpMTProxyRandomizedIntermediate
        if _mtproxy_secret.lower().startswith("dd")
        else ConnectionTcpMTProxyAbridged
    )

    class _EnvironmentMTProxyConnection(_mtproxy_base):
        """Bind Telethon's MTProxy connection to the repository env secret."""

        def __init__(
            self,
            ip,
            port,
            dc_id,
            *,
            loggers,
            proxy=None,
            local_addr=None,
        ):
            super().__init__(
                ip,
                port,
                dc_id,
                loggers=loggers,
                proxy=(_mtproxy_host, _mtproxy_port, _mtproxy_secret),
                local_addr=local_addr,
            )

    _tcpabridged.ConnectionTcpAbridged = _EnvironmentMTProxyConnection
    print(
        "Telegram download route: MTProto proxy enabled "
        f"host={_mtproxy_host} port={_mtproxy_port} "
        f"protocol={'randomized-intermediate' if _mtproxy_base is ConnectionTcpMTProxyRandomizedIntermediate else 'abridged'}",
        flush=True,
    )
elif _transport == "full":
    import telethon.network.connection.tcpabridged as _tcpabridged
    from telethon.network.connection.tcpfull import ConnectionTcpFull

    _tcpabridged.ConnectionTcpAbridged = ConnectionTcpFull


# Safe Telegram CDN detection for the bot downloader.
#
# Telegram's upload.getFile accepts cdn_supported for both users and bots, but
# a FileCdnRedirect cannot be completed by a bot because upload.getCdnFile is
# user-only. Telethon 1.40.0 already detects this condition and raises a
# ValueError. We opt into the CDN capability flag for the first request of
# each iterator, log the redirect when Telegram offers one, then immediately
# retry the same request with cdn_supported disabled. Normal downloads keep
# using the existing master-DC path and are otherwise untouched.
_CDN_DETECTION_ENABLED = os.getenv("TELEGRAM_CDN_DETECTION", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

if _CDN_DETECTION_ENABLED:
    from telethon.client.downloads import _DirectDownloadIter

    _original_download_request = _DirectDownloadIter._request

    async def _safe_cdn_detection_request(self):
        if getattr(self, "_file_to_link_cdn_probe_done", False):
            return await _original_download_request(self)

        self._file_to_link_cdn_probe_done = True
        request = getattr(self, "request", None)
        if request is None or not hasattr(request, "cdn_supported"):
            return await _original_download_request(self)

        request.cdn_supported = True
        try:
            return await _original_download_request(self)
        except ValueError as exc:
            message = str(exc)
            if "FileCdnRedirect but the GetCdnFileRequest API access for bot users is restricted" not in message:
                raise

            location = getattr(request, "location", None)
            document_id = getattr(location, "id", None)
            print(
                "TELEGRAM CDN REDIRECT DETECTED: bot client cannot consume "
                f"the CDN route for document={document_id}; falling back to master DC.",
                flush=True,
            )
            request.cdn_supported = False
            return await _original_download_request(self)
        finally:
            request.cdn_supported = False

    _DirectDownloadIter._request = _safe_cdn_detection_request
    print("Telegram CDN detection: enabled with safe bot fallback", flush=True)
else:
    print("Telegram CDN detection: disabled", flush=True)


print(
    f"Telegram download transport profile: {_transport}",
    flush=True,
)
