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


# The downloader imports ConnectionTcpAbridged directly. Map that import to a
# selectable transport here so the existing downloader can be benchmarked
# against different MTProto TCP profiles without changing its core logic.
# The current experiment defaults to Obfuscated2 because Full and Abridged
# both reproduced the same ~5.3 MB/s ceiling on the GitHub runner.
_transport = os.getenv("TELEGRAM_DOWNLOAD_TRANSPORT", "obfuscated2").strip().lower()
if _transport not in {"full", "abridged", "obfuscated2"}:
    raise ValueError(
        "TELEGRAM_DOWNLOAD_TRANSPORT must be 'full', 'abridged', or 'obfuscated2'"
    )

if _transport == "full":
    import telethon.network.connection.tcpabridged as _tcpabridged
    from telethon.network.connection.tcpfull import ConnectionTcpFull

    _tcpabridged.ConnectionTcpAbridged = ConnectionTcpFull
elif _transport == "obfuscated2":
    import telethon.network.connection.tcpabridged as _tcpabridged
    from telethon.network.connection.tcpobfuscated2 import ConnectionTcpObfuscated2

    _tcpabridged.ConnectionTcpAbridged = ConnectionTcpObfuscated2

print(
    f"Telegram download transport profile: {_transport}",
    flush=True,
)
