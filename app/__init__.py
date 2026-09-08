"""Application package initialization and Telegram download policy."""

import os


# A Telegram document is physically served from its document DC. Forcing a
# different DC at the application level can make Telethon start on the wrong
# DC and then fail when it tries to migrate back to the document DC that is
# also the current session DC (DcIdInvalidError). Keep the workflow input
# harmless by normalizing any forced value back to Telethon's safe auto mode.
_requested_dc = os.getenv("TELEGRAM_DOWNLOAD_DC", "auto").strip().lower()
if _requested_dc not in {"", "auto"}:
    print(
        "Telegram download DC override is disabled for file downloads; "
        f"requested={_requested_dc!r}, using auto/document DC instead.",
        flush=True,
    )
    os.environ["TELEGRAM_DOWNLOAD_DC"] = "auto"


# The downloader currently imports Telethon's Abridged transport directly.
# Keep that import stable while allowing transport experiments without
# rewriting the downloader. The default remains Full because it matches the
# historical high-throughput implementation. Obfuscated2 is available as a
# route/traffic-shaping experiment; it does not change the Telegram DC.
_transport = os.getenv("TELEGRAM_DOWNLOAD_TRANSPORT", "full").strip().lower()
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
