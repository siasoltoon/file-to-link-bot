"""Application package initialization and Telegram download policy."""

import os


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


# The downloader imports ConnectionTcpAbridged directly. Keep transport
# selection centralized here. Telethon 1.40.0 does not provide a
# tcpobfuscated2 module, so that experimental profile is invalid and must not
# be the default. Full and Abridged are the supported profiles used by this
# project.
_transport = os.getenv("TELEGRAM_DOWNLOAD_TRANSPORT", "full").strip().lower()
if _transport not in {"full", "abridged"}:
    raise ValueError(
        "TELEGRAM_DOWNLOAD_TRANSPORT must be 'full' or 'abridged'"
    )

if _transport == "full":
    import telethon.network.connection.tcpabridged as _tcpabridged
    from telethon.network.connection.tcpfull import ConnectionTcpFull

    _tcpabridged.ConnectionTcpAbridged = ConnectionTcpFull

print(
    f"Telegram download transport profile: {_transport}",
    flush=True,
)
