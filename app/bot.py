import asyncio
import mimetypes
import os
import secrets
import tempfile
import threading
import time
import traceback
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl import types

from .config import settings
from .storage import storage


# Telegram upload.getFile allows at most 512 KiB per request.
# One MTProto connection can still be constrained by the Telegram/DC route, so
# the downloader uses several independent authenticated MTProto connections.
# Each connection downloads a different interleaved range of the same file.
TELEGRAM_DOWNLOAD_PART_SIZE_KB = 512
TELEGRAM_DOWNLOAD_CONNECTIONS = max(
    1, min(int(os.getenv("TELEGRAM_DOWNLOAD_CONNECTIONS", "4")), 8)
)


class _ProgressReporter:
    """Throttle Telegram status edits while supporting callbacks from worker threads."""

    def __init__(self, status_message, loop, total: int, prefix: str) -> None:
        self.status_message = status_message
        self.loop = loop
        self.total = max(int(total or 0), 1)
        self.prefix = prefix
        self.seen = 0
        self.last_update = 0.0
        self.started_at = time.monotonic()
        self.lock = threading.Lock()
        self.pending_task = None

    def _schedule(self, current: int) -> None:
        percent = min(100, int((current / self.total) * 100))
        elapsed = max(time.monotonic() - self.started_at, 0.001)
        rate = current / elapsed
        text = (
            f"{self.prefix} {percent}% — {_human_size(current)} / "
            f"{_human_size(self.total)}\n"
            f"🚀 سرعت میانگین: {_human_rate(rate)}"
        )

        def create_edit_task() -> None:
            if self.pending_task is None or self.pending_task.done():
                self.pending_task = self.loop.create_task(self.status_message.edit(text))

        self.loop.call_soon_threadsafe(create_edit_task)

    def download_callback(self, current: int, total: int) -> None:
        with self.lock:
            self.total = max(int(total or self.total), 1)
            self.seen = int(current)
            now = time.monotonic()
            if self.seen < self.total and now - self.last_update < 4:
                return
            self.last_update = now
            current = self.seen
        self._schedule(current)

    def upload_callback(self, bytes_amount: int) -> None:
        with self.lock:
            self.seen += int(bytes_amount)
            now = time.monotonic()
            if self.seen < self.total and now - self.last_update < 4:
                return
            self.last_update = now
            current = self.seen
        self._schedule(current)


def _safe_filename(
    name: str | None,
    fallback_ext: str = "",
    fallback_stem: str = "telegram_file",
) -> str:
    raw = Path(name or f"{fallback_stem}{fallback_ext}").name
    raw = raw.replace("\x00", "_").replace("\r", "_").replace("\n", "_")
    return (raw[:500] or f"{fallback_stem}{fallback_ext}")


def _human_size(size: int | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _human_rate(bytes_per_second: float) -> str:
    return f"{_human_size(bytes_per_second)}/s"


async def _download_telegram_file(
    download_clients,
    message,
    destination,
    file_size,
    progress_callback,
):
    """Download a Telegram document over independent MTProto connections.

    Telegram limits each upload.getFile request to 512 KiB. Independent
    authenticated MTProto connections are used instead of many concurrent
    requests on one connection. Each connection owns one interleaved range.
    The connection count is configurable from TELEGRAM_DOWNLOAD_CONNECTIONS
    and is capped at eight to avoid creating unnecessary MTProto sessions.
    """
    document = getattr(message, "document", None)
    if not isinstance(document, types.Document):
        # Keep the normal Telethon path for media types that are not documents.
        return await message.download_media(
            file=destination,
            progress_callback=progress_callback,
        )

    location = types.InputDocumentFileLocation(
        id=document.id,
        access_hash=document.access_hash,
        file_reference=document.file_reference,
        thumb_size="",
    )
    msg_data = (message.input_chat, message.id) if message.input_chat else None

    part_size = TELEGRAM_DOWNLOAD_PART_SIZE_KB * 1024
    connection_count = max(
        1,
        min(
            TELEGRAM_DOWNLOAD_CONNECTIONS,
            (file_size + part_size - 1) // part_size,
        ),
    )
    stride = connection_count * part_size
    downloaded_total = 0
    progress_lock = asyncio.Lock()

    # Pre-create the destination so concurrent random-access writes produce the
    # expected final file even when chunks arrive out of order.
    with open(destination, "wb") as output:
        output.truncate(file_size)

    async def worker(worker_index: int, client) -> None:
        nonlocal downloaded_total
        offset = worker_index * part_size

        iterator = client._iter_download(
            location,
            offset=offset,
            stride=stride,
            chunk_size=part_size,
            request_size=part_size,
            file_size=file_size,
            msg_data=msg_data,
        )

        try:
            async for chunk in iterator:
                chunk = bytes(chunk)
                if not chunk:
                    break

                # Each worker writes to a distinct offset. The synchronous file
                # operation is small and avoids a separate thread per chunk.
                with open(destination, "r+b") as output:
                    output.seek(offset)
                    output.write(chunk)

                offset += stride
                async with progress_lock:
                    downloaded_total += len(chunk)
                    current = downloaded_total
                progress_callback(current, file_size)
        finally:
            await iterator.close()

    clients = list(download_clients[:connection_count])
    if len(clients) != connection_count:
        raise RuntimeError(
            f"Not enough Telegram download connections: "
            f"need {connection_count}, have {len(clients)}"
        )

    await asyncio.gather(
        *(worker(index, client) for index, client in enumerate(clients))
    )
    return destination


async def _process_media(event, download_clients) -> None:
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

    status = await event.reply("⏳ فایل دریافت شد؛ در حال آماده‌سازی...")
    temp_path = None
    loop = asyncio.get_running_loop()

    try:
        ext = getattr(tg_file, "ext", None) or ""
        original_name = getattr(tg_file, "name", None)
        if not original_name:
            mime_type = getattr(tg_file, "mime_type", None) or ""
            guessed_ext = mimetypes.guess_extension(mime_type) or ext or ".bin"
            if message.raw_text and not message.raw_text.startswith("/"):
                fallback_stem = _safe_filename(message.raw_text[:80], "").rsplit(".", 1)[0]
                fallback_stem = fallback_stem or "telegram_file"
            else:
                fallback_stem = f"telegram_file_{message.id}"
            filename = _safe_filename(None, guessed_ext, fallback_stem)
        else:
            filename = _safe_filename(original_name, ext)

        content_type = getattr(tg_file, "mime_type", None) or mimetypes.guess_type(filename)[0]
        temp_dir = tempfile.mkdtemp(prefix="file_to_link_")
        temp_path = os.path.join(temp_dir, filename)

        if size:
            download_progress = _ProgressReporter(status, loop, size, "⏬ دانلود از تلگرام")
            downloaded = await _download_telegram_file(
                download_clients,
                message,
                temp_path,
                size,
                download_progress.download_callback,
            )
        else:
            await status.edit("⏬ در حال دانلود فایل از تلگرام...")
            downloaded = await message.download_media(
                file=temp_path,
            )

        if not downloaded or not os.path.isfile(downloaded):
            raise RuntimeError("Telegram media download did not produce a local file")

        actual_size = os.path.getsize(downloaded)
        if actual_size > settings.max_file_bytes:
            raise RuntimeError("Downloaded file exceeds configured maximum size")

        await status.edit(
            "⬆️ فایل دریافت شد؛ در حال آپلود به فضای ابری...\n"
            f"📁 {filename}\n"
            f"💾 {_human_size(actual_size)}"
        )

        token = secrets.token_urlsafe(24)
        object_key = f"files/{token}/{filename}"
        upload_progress = _ProgressReporter(status, loop, actual_size, "⬆️ آپلود به فضای ابری")
        await asyncio.to_thread(
            storage.upload_file,
            downloaded,
            object_key,
            content_type,
            upload_progress.upload_callback,
        )

        await status.edit("🔗 آپلود کامل شد؛ در حال ساخت لینک دانلود مستقیم...")
        link = await asyncio.to_thread(
            storage.presigned_download_url,
            object_key,
            filename,
            None,
            content_type,
        )

        await status.edit(
            "✅ لینک دانلود مستقیم آماده شد!\n\n"
            f"📁 {filename}\n"
            f"💾 {_human_size(actual_size)}\n"
            f"⏳ اعتبار لینک: {settings.direct_link_expires_seconds // 86400} روز\n\n"
            f"🔗 {link}"
        )
    except Exception as exc:
        print(f"file processing error: {exc!r}", flush=True)
        print(traceback.format_exc(), flush=True)
        try:
            await status.edit("❌ پردازش فایل ناموفق بود. لاگ VPS را بررسی کنید و دوباره تلاش کنید.")
        except Exception:
            pass
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

    try:
        import cryptg  # noqa: F401
        print("Telethon crypto acceleration: cryptg enabled", flush=True)
    except ImportError:
        print("WARNING: cryptg is not installed; Telethon will use slower pure-Python crypto", flush=True)

    client = TelegramClient(
        "file-to-link-bot",
        settings.telegram_api_id,
        settings.telegram_api_hash,
        request_retries=5,
        connection_retries=5,
        retry_delay=3,
        auto_reconnect=True,
    )

    download_clients = []
    try:
        for index in range(TELEGRAM_DOWNLOAD_CONNECTIONS):
            downloader = TelegramClient(
                f"file-to-link-bot-download-{index}",
                settings.telegram_api_id,
                settings.telegram_api_hash,
                request_retries=5,
                connection_retries=5,
                retry_delay=3,
                auto_reconnect=True,
            )
            await downloader.start(bot_token=settings.bot_token)
            download_clients.append(downloader)
            print(
                f"Telegram download connection {index + 1}/{TELEGRAM_DOWNLOAD_CONNECTIONS} ready",
                flush=True,
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
                await _process_media(event, download_clients)

        await client.start(bot_token=settings.bot_token)
        me = await client.get_me()
        print(f"Bot started as @{getattr(me, 'username', None) or me.id}", flush=True)
        print(
            f"Telegram downloader: {TELEGRAM_DOWNLOAD_CONNECTIONS} independent MTProto connections "
            f"× {TELEGRAM_DOWNLOAD_PART_SIZE_KB} KiB",
            flush=True,
        )
        await client.run_until_disconnected()
    finally:
        if client.is_connected():
            await client.disconnect()
        for downloader in download_clients:
            if downloader.is_connected():
                await downloader.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
