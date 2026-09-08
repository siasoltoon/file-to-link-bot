"""Application package initialization and Telegram download policy."""

import os


_requested_dc = os.getenv("TELEGRAM_DOWNLOAD_DC", "auto").strip().lower()
if _requested_dc not in {"", "auto"}:
    print(
        "Telegram download DC override is disabled for file downloads; "
        f"requested={_requested_dc!r}, using auto/document DC instead.",
        flush=True,
    )
    os.environ["TELEGRAM_DOWNLOAD_DC"] = "auto"


_transport = os.getenv("TELEGRAM_DOWNLOAD_TRANSPORT", "full").strip().lower()
if _transport not in {"full", "abridged"}:
    raise ValueError("TELEGRAM_DOWNLOAD_TRANSPORT must be 'full' or 'abridged'")


import telethon.network.connection.tcpabridged as _tcpabridged

if _transport == "full":
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
