import asyncio
import os
import secrets
from datetime import timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import settings
from .db import StoredFile, add_file, init_db, utcnow
from .storage import storage

MAX_FILE_BYTES = settings.max_file_size_mb * 1024 * 1024
upload_semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_uploads))


def _file_from_message(message):
    for attr in ("document", "video", "audio", "animation"):
        value = getattr(message, attr, None)
        if value is not None:
            return value
    return None


def _safe_filename(name: str | None) -> str:
    name = name or "file"
    name = Path(name).name.replace("\x00", "_").strip()
    return name[:500] or "file"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "سلام 👋\n\n"
            "فایل خودت را همین‌جا ارسال کن تا یک لینک دانلود مستقیم برایت بسازم.\n"
            f"حداکثر اندازه فایل: {settings.max_file_size_mb}MB"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "📖 راهنما\n\n"
            "یک فایل، ویدئو، صدا یا انیمیشن ارسال کن.\n"
            "ربات فایل را در فضای ذخیره‌سازی نگه می‌دارد و لینک دانلود مستقیم می‌سازد.\n\n"
            f"⏳ فایل‌های کاربران عادی بعد از {settings.default_file_ttl_days} روز حذف می‌شوند."
        )


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
        await message.reply_text(
            f"❌ حجم فایل بیشتر از سقف {settings.max_file_size_mb}MB است."
        )
        return

    status = await message.reply_text("⏳ فایل دریافت شد؛ در حال ساخت لینک مستقیم...")
    local_path: str | None = None
    object_key: str | None = None
    uploaded = False

    try:
        async with upload_semaphore:
            telegram_file = await context.bot.get_file(tg_file.file_id)
            local_path = telegram_file.file_path
            if not local_path or not os.path.isfile(local_path):
                raise RuntimeError("Local Bot API did not return a usable local file path")

            actual_size = os.path.getsize(local_path)
            if actual_size > MAX_FILE_BYTES:
                raise ValueError("Downloaded file exceeds configured maximum size")

            filename = _safe_filename(getattr(tg_file, "file_name", None))
            token = secrets.token_urlsafe(32)
            object_key = f"files/{token}/{filename}"
            content_type = getattr(tg_file, "mime_type", None)

            await asyncio.to_thread(
                storage.upload_file,
                local_path,
                object_key,
                content_type,
            )
            uploaded = True

            now = utcnow()
            is_owner = user.id == settings.owner_telegram_id
            expires_at = None if is_owner else now + timedelta(
                days=settings.default_file_ttl_days
            )

            record = StoredFile(
                link_token=token,
                owner_telegram_id=user.id,
                filename=filename,
                content_type=content_type,
                size_bytes=size or actual_size,
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
                f"اعتبار لینک: {lifetime}"
            )
    except Exception as exc:
        if uploaded and object_key:
            try:
                await asyncio.to_thread(storage.delete_file, object_key)
            except Exception as cleanup_exc:
                print(f"upload rollback failed: {cleanup_exc!r}")
        print(f"file processing error: {exc!r}")
        try:
            await status.edit_text(
                "❌ پردازش فایل ناموفق بود.\n"
                "لطفاً دوباره تلاش کنید."
            )
        except Exception:
            pass
    finally:
        if local_path:
            try:
                path = Path(local_path)
                if path.is_file():
                    path.unlink()
            except Exception as cleanup_exc:
                print(f"local file cleanup failed: {cleanup_exc!r}")


def build_application() -> Application:
    init_db()
    application = (
        Application.builder()
        .token(settings.bot_token)
        .base_url(settings.telegram_api_base_url)
        .base_file_url(settings.telegram_file_base_url)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(
            filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.ANIMATION,
            handle_file,
        )
    )
    return application


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
