import asyncio
import mimetypes
import os
import secrets
import tempfile
from pathlib import Path

from telethon import TelegramClient, events

from .config import settings
from .storage import storage


def _safe_filename(name: str | None, fallback_ext: str = "") -> str:
    raw = Path(name or f"file{fallback_ext}").name
    raw = raw.replace("\x00", "_").replace("\r", "_").replace("\n", "_")
    return (raw[:500] or f"file{fallback_ext}")


def _human_size(size: int | None) -> str:
    if not size:
        return "نامشخص"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


async def _process_media(event) -> None:
    message = event.message
    if not message or not message.media:
        return

    tg_file = message.file
    if tg_file is None:
        return

    size = getattr(tg_file, "size", None)
    if size is not None and size > settings.max_file_bytes:
        await event.reply("❌ حجم فایل بیشتر از سقف مجاز این ربات است.")
        return

    status = await event.reply("⏳ فایل دریافت شد؛ در حال دانلود و ساخت لینک مستقیم...")
    temp_path = None

    try:
        ext = getattr(tg_file, "ext", None) or ""
        filename = _safe_filename(getattr(tg_file, "name", None), ext)
        content_type = getattr(tg_file, "mime_type", None) or mimetypes.guess_type(filename)[0]

        temp_dir = tempfile.mkdtemp(prefix="file_to_link_")
        temp_path = os.path.join(temp_dir, filename)

        downloaded = await message.download_media(file=temp_path)
        if not downloaded or not os.path.isfile(downloaded):
            raise RuntimeError("Telegram media download did not produce a local file")

        actual_size = os.path.getsize(downloaded)
        if actual_size > settings.max_file_bytes:
            raise RuntimeError("Downloaded file exceeds configured maximum size")

        token = secrets.token_urlsafe(24)
        object_key = f"files/{token}/{filename}"
        await asyncio.to_thread(storage.upload_file, downloaded, object_key, content_type)
        link = await asyncio.to_thread(storage.presigned_download_url, object_key, filename)

        await status.edit(
            "✅ لینک دانلود مستقیم آماده شد!\n\n"
            f"📁 {filename}\n"
            f"💾 {_human_size(actual_size)}\n"
            f"⏳ اعتبار لینک: {settings.direct_link_expires_seconds // 86400} روز\n\n"
            f"🔗 {link}"
        )
    except Exception as exc:
        try:
            await status.edit("❌ پردازش فایل ناموفق بود. لاگ VPS را بررسی کنید و دوباره تلاش کنید.")
        except Exception:
            pass
        print(f"file processing error: {exc!r}", flush=True)
    finally:
        if temp_path:
            try:
                parent = Path(temp_path).parent
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                if parent.exists():
                    parent.rmdir()
            except OSError:
                pass


async def main() -> None:
    storage.healthcheck()

    client = TelegramClient(
        "file-to-link-bot",
        settings.telegram_api_id,
        settings.telegram_api_hash,
        request_retries=5,
        connection_retries=5,
        retry_delay=3,
        auto_reconnect=True,
    )

    @client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    async def start_handler(event):
        await event.reply(
            "سلام 👋\n"
            "فایل، ویدیو، صدا یا هر مدیایی را برای من بفرست. "
            "آن را روی فضای ذخیره‌سازی آپلود می‌کنم و لینک دانلود مستقیم می‌دهم."
        )

    @client.on(events.NewMessage(incoming=True))
    async def media_handler(event):
        if event.raw_text and event.raw_text.startswith("/start"):
            return
        if event.message and event.message.media:
            await _process_media(event)

    await client.start(bot_token=settings.bot_token)
    me = await client.get_me()
    print(f"Bot started as @{getattr(me, 'username', None) or me.id}", flush=True)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
