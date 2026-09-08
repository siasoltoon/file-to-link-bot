"""Application package initialization and Telegram DC safety policy."""

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
