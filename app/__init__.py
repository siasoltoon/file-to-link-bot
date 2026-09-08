"""Application package initialization and Telegram download policy."""

import base64
import binascii
import os
import re
from urllib.parse import parse_qs, unquote, urlparse


_requested_dc = os.getenv("TELEGRAM_DOWNLOAD_DC", "auto").strip().lower()
if _requested_dc not in {"", "auto"}:
    print(
        "Telegram download DC override is disabled for file downloads; "
        f"requested={_requested_dc!r}, using auto/document DC instead.",
        flush=True,
    )
    os.environ["TELEGRAM_DOWNLOAD_DC"] = "auto"


_proxy_url = os.getenv("TELEGRAM_DOWNLOAD_PROXY_URL", "").strip()
_mtproxy_config = None


def _parse_mtproxy_url(value: str):
    """Parse common MTProto/MTProxy links without logging the secret."""
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
        secret = unquote(parsed.path.lstrip("/")) if parsed.path and parsed.path != "/" else ""
        if not secret:
            query = parse_qs(parsed.query)
            secret = unquote(query.get("secret", [""])[0]).strip()
        if host and port and secret:
            return host, port, secret
    return None


def _decode_base64_secret(value: str):
    """Decode a standard base64 MTProxy secret when it represents 16 bytes."""
    cleaned = re.sub(r"\s+", "", value)
    if not cleaned or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", cleaned):
        return None
    try:
        padded = cleaned + "=" * (-len(cleaned) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return None
    return decoded if len(decoded) == 16 else None


def _classify_mtproxy_secret(secret: str):
    """Return (transport, secret_for_telethon) without exposing the secret.

    ``ee`` at the beginning of the *text* is not by itself proof of Fake-TLS.
    A normal 16-byte base64 MTProxy secret can legitimately begin with the
    characters ``ee``. Fake-TLS is selected only for a structurally valid
    Fake-TLS secret: an ``ee`` hex secret containing a key plus domain, or a
    base64 Fake-TLS secret beginning with ``7`` that decodes to at least
    17 bytes.
    """
    value = secret.strip()
    lower = value.lower()

    if lower.startswith("ee"):
        payload = value[2:]
        if len(payload) >= 32 and len(payload) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", payload):
            decoded = bytes.fromhex("ee" + payload)
            if len(decoded) >= 17 and decoded[17:]:
                return "mtproxy-faketls", value

    if lower.startswith("7"):
        decoded = _decode_base64_secret(value[1:])
        if decoded is not None:
            return "mtproxy-faketls", value
        try:
            padded = value + "=" * (-len(value) % 4)
            decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            decoded = None
        if decoded is not None and len(decoded) >= 17:
            return "mtproxy-faketls", value

    if len(value) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return "mtproxy", value

    if lower.startswith("dd") and len(value) == 34 and re.fullmatch(r"[0-9a-fA-F]{34}", value):
        return "mtproxy", value

    decoded = _decode_base64_secret(value)
    if decoded is not None:
        # Convert base64 to hex before handing it to Telethon. This avoids
        # Telethon mistaking a perfectly valid base64 secret beginning with
        # the text "ee" for an EE Fake-TLS marker.
        return "mtproxy", decoded.hex()

    raise ValueError(
        "Unsupported MTProxy secret format. Expected a 16-byte hex/base64 "
        "secret or a structurally valid Fake-TLS secret."
    )


_mtproxy_config = _parse_mtproxy_url(_proxy_url)
if _mtproxy_config is not None:
    _mtproxy_host, _mtproxy_port, _mtproxy_secret = _mtproxy_config
    _mtproxy_transport, _mtproxy_secret_for_telethon = _classify_mtproxy_secret(_mtproxy_secret)
    os.environ["TELEGRAM_DOWNLOAD_PROXY_URL"] = f"http://{_mtproxy_host}:{_mtproxy_port}"
else:
    _mtproxy_transport = None
    _mtproxy_secret_for_telethon = None


_transport = os.getenv("TELEGRAM_DOWNLOAD_TRANSPORT", "full").strip().lower()
if _mtproxy_config is not None:
    _transport = _mtproxy_transport
elif _transport not in {"full", "abridged"}:
    raise ValueError("TELEGRAM_DOWNLOAD_TRANSPORT must be 'full' or 'abridged'")


import telethon.network.connection.tcpabridged as _tcpabridged

if _mtproxy_config is not None:
    if _mtproxy_transport == "mtproxy-faketls":
        from app.mtproxy_faketls import ConnectionTcpMTProxyFakeTLS

        class _EnvironmentMTProxyFakeTLSConnection(ConnectionTcpMTProxyFakeTLS):
            @staticmethod
            def address_info(proxy_info):
                if isinstance(proxy_info, dict):
                    return proxy_info["addr"], proxy_info["port"]
                return proxy_info[:2]

            def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
                super().__init__(
                    ip,
                    port,
                    dc_id,
                    loggers=loggers,
                    proxy=(_mtproxy_host, _mtproxy_port, _mtproxy_secret),
                    local_addr=local_addr,
                )

        _tcpabridged.ConnectionTcpAbridged = _EnvironmentMTProxyFakeTLSConnection
        print(
            "Telegram download route: MTProto Fake-TLS proxy enabled "
            f"host={_mtproxy_host} port={_mtproxy_port}",
            flush=True,
        )
    else:
        from telethon.network.connection.tcpmtproxy import (
            ConnectionTcpMTProxyAbridged,
            ConnectionTcpMTProxyRandomizedIntermediate,
        )

        _mtproxy_base = (
            ConnectionTcpMTProxyRandomizedIntermediate
            if _mtproxy_secret_for_telethon.lower().startswith("dd")
            else ConnectionTcpMTProxyAbridged
        )

        class _EnvironmentMTProxyConnection(_mtproxy_base):
            @staticmethod
            def address_info(proxy_info):
                if isinstance(proxy_info, dict):
                    return proxy_info["addr"], proxy_info["port"]
                return proxy_info[:2]

            def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
                super().__init__(
                    ip,
                    port,
                    dc_id,
                    loggers=loggers,
                    proxy=(_mtproxy_host, _mtproxy_port, _mtproxy_secret_for_telethon),
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
    from telethon.network.connection.tcpfull import ConnectionTcpFull
    _tcpabridged.ConnectionTcpAbridged = ConnectionTcpFull


# Safe Telegram CDN detection for the bot downloader.
_CDN_DETECTION_ENABLED = os.getenv("TELEGRAM_CDN_DETECTION", "1").strip().lower() not in {
    "0", "false", "no", "off"
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

print(f"Telegram download transport profile: {_transport}", flush=True)
