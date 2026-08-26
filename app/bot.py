import asyncio
import os
import secrets
from datetime import timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from .config import settings
from .db import StoredFile, add_file, init_db, utcnow
from .storage import storage

MAX_FILE_BYTES = 2_000_000_000


def _file_from_message(message):
    for attr in ("document", "video", "audio", "animation"):
        value = getattr(message, attr, None)
        if value is not None:
            return value
    return None


def _safe_filename(name: str | None) -> str:
    name = name or "file"
    name = Path(name).name.replace("\x00", "_")
    return name[:500] or "file"


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    tg_file = _file_from_message(message)
    if tg_file is None:
        return

    size = getattr(tg_file, "file_size", None)
    if size is not None and size > MAX_FILE_BYTES:
        await message.reply_text("❌ حجم فایل بیشتر از سقف ۲GB است.")
        return

    status = await message.reply_text("⏳ فایل دریافت شد؛ در حال آماده‌سازی لینک مستقیم...")

    try:
        telegram_file = await context.bot.get_file(tg_file.file_id)
        # In Local Bot API mode, getFile returns the absolute local path.
        local_path = telegram_file.file_path
        if not local_path or not os.path.isfile(local_path):
            raise RuntimeError("Local Bot API did not return a usable local file path")

        filename = _safe_filename(getattr(tg_file, "file_name", None))
        token = secrets.token_urlsafe(24)
        object_key = f"files/{token}/{filename}"
        content_type = getattr(tg_file, "mime_type", None)

        await asyncio.to_thread(storage.upload_file, local_path, object_key, content_type)

        now = utcnow()
        is_owner = user.id == settings.owner_telegram_id
        expires_at = None if is_owner else now + timedelta(days=settings.default_file_ttl_days)

        record = StoredFile(
            link_token=token,
            owner_telegram_id=user.id,
            filename=filename,
            content_type=content_type,
            size_bytes=size or os.path.getsize(local_path),
            object_key=object_key,
            created_at=now,
            expires_at=expires_at,
        )
        add_file(record)

        link = f"{settings.public_base_url.rstrip('/')}/d/{token}"
        lifetime = "♾️ دائمی" if is_owner else f"⏳ {settings.default_file_ttl_days} روز"
        await status.edit_text(
            "✅ فایل آماده شد!\n\n"
            f"📁 {filename}\n"
            f"💾 {record.size_bytes / 1024 / 1024:.1f} MB\n"
            f"🔗 {link}\n\n"
            f"اعتبار: {lifetime}"
        )
    except Exception as exc:
        await status.edit_text("❌ پردازش فایل ناموفق بود. لطفاً دوباره تلاش کنید.")
        print(f"file processing error: {exc!r}")


def build_application() -> Application:
    init_db()
    application = (
        Application.builder()
        .token(settings.bot_token)
        .base_url(settings.telegram_api_base_url)
        .base_file_url(settings.telegram_file_base_url)
        .build()
    )
    application.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.ANIMATION, handle_file))
    return application


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
