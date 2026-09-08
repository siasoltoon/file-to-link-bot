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


# Transport selection is intentionally centralized here because app.bot imports
# ConnectionTcpAbridged directly. The historical high-throughput downloader
# used Telethon's default TCP Full transport, while the later Abridged switch
# reduced protocol overhead but did not reproduce the historical peak rate on
# this Windows runner. Keep Full as the production default, while allowing a
# controlled Abridged fallback through TELEGRAM_DOWNLOAD_TRANSPORT=abridged.
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
