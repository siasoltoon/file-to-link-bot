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
from telethon.errors import MessageNotModifiedError
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged
from telethon.sessions import MemorySession
from telethon.tl import types

from .config import settings
from .storage import storage


# Telegram upload.getFile allows at most 512 KiB per request.
# Keep a pool of independent MTProto connections. In the normal 4x4 setup
# every worker gets its own connection, while larger worker counts can safely
# share the pool. The downloader connections do not receive bot updates.
TELEGRAM_DOWNLOAD_PART_SIZE_KB = 512
TELEGRAM_DOWNLOAD_CONNECTIONS = max(
    1, min(int(os.getenv("TELEGRAM_DOWNLOAD_CONNECTIONS", "8")), 8)
)
TELEGRAM_DOWNLOAD_WORKERS = max(
    1, min(int(os.getenv("TELEGRAM_DOWNLOAD_WORKERS", "32")), 32)
)
TELEGRAM_DOWNLOAD_DC = os.getenv("TELEGRAM_DOWNLOAD_DC", "auto").strip().lower()
TELEGRAM_DOWNLOAD_LOG_INTERVAL_SECONDS = max(
    2.0, float(os.getenv("TELEGRAM_DOWNLOAD_LOG_INTERVAL_SECONDS", "5"))
)
TELEGRAM_DOWNLOAD_STALL_SECONDS = max(
    10.0, float(os.getenv("TELEGRAM_DOWNLOAD_STALL_SECONDS", "15"))
)
TELEGRAM_DOWNLOAD_REQUEST_TIMEOUT_SECONDS = max(
    8.0, float(os.getenv("TELEGRAM_DOWNLOAD_REQUEST_TIMEOUT_SECONDS", "12"))
)
TELEGRAM_DOWNLOAD_MAX_RECOVERIES = max(
    2, int(os.getenv("TELEGRAM_DOWNLOAD_MAX_RECOVERIES", "12"))
)
TELEGRAM_DOWNLOAD_RECONNECT_TIMEOUT_SECONDS = max(
    5.0, float(os.getenv("TELEGRAM_DOWNLOAD_RECONNECT_TIMEOUT_SECONDS", "15"))
)
TELEGRAM_DOWNLOAD_RECOVERY_COOLDOWN_SECONDS = max(
    0.5, float(os.getenv("TELEGRAM_DOWNLOAD_RECOVERY_COOLDOWN_SECONDS", "3"))
)


class _ProgressReporter:
    """Throttle Telegram status edits and suppress harmless duplicate edits."""

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
        self.last_text = None

    def _schedule(self, current: int) -> None:
        percent = min(100, int((current / self.total) * 100))
        elapsed = max(time.monotonic() - self.started_at, 0.001)
        rate = current / elapsed
        text = (
            f"{self.prefix} {percent}% — {_human_size(current)} / "
            f"{_human_size(self.total)}\n"
            f"🚀 سرعت میانگین: {_human_rate(rate)}"
        )

        if text == self.last_text:
            return
        self.last_text = text

        async def edit_status() -> None:
            try:
                await self.status_message.edit(text)
            except MessageNotModifiedError:
                pass
            except Exception as exc:
                print(f"progress status edit warning: {exc!r}", flush=True)

        def create_edit_task() -> None:
            if self.pending_task is None or self.pending_task.done():
                self.pending_task = self.loop.create_task(edit_status())

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


def _safe_filename(
    name: str | None,
    fallback_ext: str = "",
    fallback_stem: str = "telegram_file",
) -> str:
    raw = Path(name or f"{fallback_stem}{fallback_ext}").name
    raw = raw.replace("\x00", "_").replace("\r", "_").replace("\n", "_")
    return (raw[:500] or f"{fallback_stem}{fallback_ext}")


async def _download_telegram_file(
    download_clients,
    message,
    destination,
    file_size,
    progress_callback,
):
    """Download a Telegram document with parallel workers and self-healing connections."""
    document = getattr(message, "document", None)
    if not isinstance(document, types.Document):
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
    document_dc_id = getattr(document, "dc_id", None)
    if TELEGRAM_DOWNLOAD_DC == "auto":
        requested_dc_id = document_dc_id
    else:
        try:
            requested_dc_id = int(TELEGRAM_DOWNLOAD_DC)
        except ValueError as exc:
            raise ValueError(
                "TELEGRAM_DOWNLOAD_DC must be 'auto' or a numeric Telegram DC id"
            ) from exc
        if requested_dc_id < 1 or requested_dc_id > 5:
            raise ValueError("TELEGRAM_DOWNLOAD_DC must be auto or one of 1,2,3,4,5")

    part_size = TELEGRAM_DOWNLOAD_PART_SIZE_KB * 1024
    worker_count = max(
        1,
        min(
            TELEGRAM_DOWNLOAD_WORKERS,
            (file_size + part_size - 1) // part_size,
        ),
    )
    connection_count = max(1, min(len(download_clients), TELEGRAM_DOWNLOAD_CONNECTIONS))
    stride = worker_count * part_size
    downloaded_total = 0
    progress_lock = asyncio.Lock()
    stats_lock = asyncio.Lock()
    assignment_lock = asyncio.Lock()
    download_started = time.monotonic()
    last_logged_bytes = 0
    last_logged_time = download_started
    last_progress_seen = 0
    worker_bytes = [0] * worker_count
    worker_last_progress = [download_started] * worker_count
    worker_connection = [i % connection_count for i in range(worker_count)]
    connection_bytes = [0] * connection_count
    connection_last_progress = [download_started] * connection_count
    connection_recoveries = [0] * connection_count
    connection_last_failure = [0.0] * connection_count
    connection_locks = [asyncio.Lock() for _ in range(connection_count)]
    stop_monitor = asyncio.Event()

    print(
        f"DOWNLOAD START: size={file_size} bytes ({_human_size(file_size)}), "
        f"document_dc={document_dc_id}, requested_dc={requested_dc_id}, "
        f"connections={connection_count}, workers={worker_count}, part={part_size} bytes, "
        f"request_timeout={TELEGRAM_DOWNLOAD_REQUEST_TIMEOUT_SECONDS:.1f}s",
        flush=True,
    )

    with open(destination, "wb") as output:
        output.truncate(file_size)

    async def log_stats(reason: str = "periodic") -> None:
        nonlocal last_logged_bytes, last_logged_time, last_progress_seen
        now = time.monotonic()
        elapsed = max(now - download_started, 0.001)
        interval_elapsed = max(now - last_logged_time, 0.001)
        interval_bytes = downloaded_total - last_logged_bytes
        average_rate = downloaded_total / elapsed
        interval_rate = interval_bytes / interval_elapsed
        percent = min(100.0, (downloaded_total / max(file_size, 1)) * 100)
        remaining = max(file_size - downloaded_total, 0)
        eta = (remaining / average_rate) if average_rate > 0 else 0

        async with stats_lock:
            cb = list(connection_bytes)
            cl = list(connection_last_progress)
            wc = list(worker_connection)
            recoveries = list(connection_recoveries)
            failures = list(connection_last_failure)

        connection_rates = [value / elapsed for value in cb]
        active_workers = sum(
            1
            for timestamp in worker_last_progress
            if now - timestamp < TELEGRAM_DOWNLOAD_STALL_SECONDS
        )
        active_connections = sum(
            1
            for timestamp in cl
            if now - timestamp < TELEGRAM_DOWNLOAD_STALL_SECONDS
        )
        print(
            f"DOWNLOAD STATS [{reason}]: {percent:.2f}% | "
            f"{_human_size(downloaded_total)}/{_human_size(file_size)} | "
            f"avg={_human_rate(average_rate)} | interval={_human_rate(interval_rate)} | "
            f"eta={eta:.1f}s | active_workers={active_workers}/{worker_count} | "
            f"active_connections={active_connections}/{connection_count}",
            flush=True,
        )
        print(
            "DOWNLOAD CONNECTIONS: "
            + " | ".join(
                f"C{i + 1}={_human_size(value)} ({_human_rate(rate)}) "
                f"last={max(0.0, now - cl[i]):.1f}s "
                f"recoveries={recoveries[i]} "
                f"cooldown={max(0.0, TELEGRAM_DOWNLOAD_RECOVERY_COOLDOWN_SECONDS - (now - failures[i])):.1f}s"
                for i, (value, rate) in enumerate(zip(cb, connection_rates))
            ),
            flush=True,
        )
        stalled_workers = [
            f"W{i + 1}(C{wc[i] + 1},{max(0.0, now - worker_last_progress[i]):.1f}s)"
            for i in range(worker_count)
            if now - worker_last_progress[i] >= TELEGRAM_DOWNLOAD_STALL_SECONDS
        ]
        if stalled_workers:
            print("DOWNLOAD STALLED WORKERS: " + ", ".join(stalled_workers), flush=True)

        last_logged_bytes = downloaded_total
        last_logged_time = now
        last_progress_seen = downloaded_total

    async def monitor() -> None:
        nonlocal last_progress_seen
        while not stop_monitor.is_set():
            try:
                await asyncio.wait_for(
                    stop_monitor.wait(),
                    timeout=TELEGRAM_DOWNLOAD_LOG_INTERVAL_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                await log_stats("periodic")
                if downloaded_total == last_progress_seen and downloaded_total < file_size:
                    stalled_for = time.monotonic() - max(worker_last_progress)
                    if stalled_for >= TELEGRAM_DOWNLOAD_STALL_SECONDS:
                        print(
                            f"DOWNLOAD GLOBAL STALL: no worker progress for {stalled_for:.1f}s",
                            flush=True,
                        )

    async def choose_recovery_connection(current_connection: int) -> int:
        async with assignment_lock:
            now = time.monotonic()
            worker_load = [0] * connection_count
            for assigned in worker_connection:
                worker_load[assigned] += 1
            candidates = [i for i in range(connection_count) if i != current_connection]
            healthy = [
                i
                for i in candidates
                if now - connection_last_failure[i] >= TELEGRAM_DOWNLOAD_RECOVERY_COOLDOWN_SECONDS
            ]
            pool = healthy or candidates
            if not pool:
                return current_connection
            pool.sort(
                key=lambda i: (
                    worker_load[i],
                    connection_recoveries[i],
                    -connection_last_progress[i],
                )
            )
            return pool[0]

    async def reconnect_connection(connection_index: int) -> None:
        client = download_clients[connection_index]
        lock = connection_locks[connection_index]
        async with lock:
            try:
                if client.is_connected():
                    await asyncio.wait_for(
                        client.disconnect(),
                        timeout=TELEGRAM_DOWNLOAD_RECONNECT_TIMEOUT_SECONDS,
                    )
            except Exception as exc:
                print(
                    f"DOWNLOAD CONNECTION DISCONNECT WARNING: C{connection_index + 1} {exc!r}",
                    flush=True,
                )
            try:
                await asyncio.wait_for(
                    client.connect(),
                    timeout=TELEGRAM_DOWNLOAD_RECONNECT_TIMEOUT_SECONDS,
                )
                if not client.is_connected():
                    raise RuntimeError("connection did not report connected state")
                print(
                    f"DOWNLOAD CONNECTION RECONNECTED: C{connection_index + 1} "
                    f"dc={client.session.dc_id}",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"DOWNLOAD CONNECTION RECONNECT FAILED: C{connection_index + 1} {exc!r}",
                    flush=True,
                )
                raise

    def is_recoverable_download_error(exc: BaseException) -> bool:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError, ConnectionError)):
            return True
        if isinstance(exc, ValueError) and "Request was unsuccessful" in str(exc):
            return True
        if isinstance(exc, RuntimeError) and "download iterator ended early" in str(exc):
            return True
        return False

    async def recover_worker(
        worker_index: int,
        current_connection: int,
        offset: int,
        recoveries: int,
        reason: BaseException,
    ) -> tuple[int, int]:
        if recoveries >= TELEGRAM_DOWNLOAD_MAX_RECOVERIES:
            raise RuntimeError(
                f"worker {worker_index + 1} exceeded maximum recovery attempts "
                f"({TELEGRAM_DOWNLOAD_MAX_RECOVERIES}) at offset {offset}: {reason!r}"
            ) from reason

        async with stats_lock:
            connection_last_failure[current_connection] = time.monotonic()

        # Reset the failed transport before reusing the pool. This prevents a
        # half-open MTProto socket from being selected repeatedly after a timeout.
        try:
            await reconnect_connection(current_connection)
        except Exception:
            pass

        new_connection = await choose_recovery_connection(current_connection)
        async with stats_lock:
            worker_connection[worker_index] = new_connection
            connection_recoveries[current_connection] += 1
        recoveries += 1
        print(
            f"DOWNLOAD CONNECTION RECOVERY: worker={worker_index + 1}/{worker_count} "
            f"C{current_connection + 1}->C{new_connection + 1} "
            f"offset={offset} recovery={recoveries} reason={reason!r}",
            flush=True,
        )
        await asyncio.sleep(0.15)
        return new_connection, recoveries

    async def worker(worker_index: int) -> None:
        nonlocal downloaded_total
        offset = worker_index * part_size
        worker_started = time.monotonic()
        recoveries = 0

        with open(destination, "r+b", buffering=0) as output:
            while offset < file_size:
                async with stats_lock:
                    connection_index = worker_connection[worker_index]
                client = download_clients[connection_index]
                if not client.is_connected():
                    await reconnect_connection(connection_index)

                iterator = client._iter_download(
                    location,
                    offset=offset,
                    stride=stride,
                    chunk_size=part_size,
                    request_size=part_size,
                    file_size=file_size,
                    msg_data=msg_data,
                    dc_id=requested_dc_id,
                )
                iterator_closed = False
                try:
                    while offset < file_size:
                        try:
                            chunk = await asyncio.wait_for(
                                iterator.__anext__(),
                                timeout=TELEGRAM_DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
                            )
                        except StopAsyncIteration:
                            break
                        chunk = bytes(chunk)
                        if not chunk:
                            break

                        output.seek(offset)
                        output.write(chunk)
                        offset += stride

                        now = time.monotonic()
                        async with progress_lock:
                            downloaded_total += len(chunk)
                            current = downloaded_total
                        async with stats_lock:
                            worker_bytes[worker_index] += len(chunk)
                            worker_last_progress[worker_index] = now
                            connection_bytes[connection_index] += len(chunk)
                            connection_last_progress[connection_index] = now
                        progress_callback(current, file_size)

                    if offset >= file_size:
                        break
                    raise RuntimeError(
                        f"download iterator ended early at offset={offset}"
                    )
                except Exception as exc:
                    if is_recoverable_download_error(exc):
                        try:
                            await asyncio.wait_for(
                                iterator.close(),
                                timeout=3.0,
                            )
                            iterator_closed = True
                        except Exception:
                            pass
                        connection_index, recoveries = await recover_worker(
                            worker_index,
                            connection_index,
                            offset,
                            recoveries,
                            exc,
                        )
                        continue

                    print(
                        f"DOWNLOAD WORKER ERROR: worker={worker_index + 1}/{worker_count} "
                        f"connection={connection_index + 1}/{connection_count} "
                        f"offset={offset} error={exc!r}",
                        flush=True,
                    )
                    print(traceback.format_exc(), flush=True)
                    raise
                finally:
                    if not iterator_closed:
                        try:
                            await asyncio.wait_for(iterator.close(), timeout=3.0)
                        except Exception:
                            pass

        elapsed = max(time.monotonic() - worker_started, 0.001)
        print(
            f"DOWNLOAD WORKER FINISHED: worker={worker_index + 1}/{worker_count} "
            f"bytes={worker_bytes[worker_index]} ({_human_size(worker_bytes[worker_index])}) "
            f"avg={_human_rate(worker_bytes[worker_index] / elapsed)} "
            f"recoveries={recoveries}",
            flush=True,
        )

    clients = list(download_clients[:connection_count])
    if not clients:
        raise RuntimeError("No Telegram download connections are available")

    monitor_task = asyncio.create_task(monitor())
    tasks = [asyncio.create_task(worker(index)) for index in range(worker_count)]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise RuntimeError(
                f"Telegram download failed in {len(errors)} worker(s): {errors[0]!r}"
            )
        if downloaded_total != file_size:
            raise RuntimeError(
                f"Telegram download incomplete: received {downloaded_total} of {file_size} bytes"
            )
        await log_stats("FINAL")
        elapsed = max(time.monotonic() - download_started, 0.001)
        print(
            f"DOWNLOAD COMPLETE: {_human_size(downloaded_total)} in {elapsed:.1f}s "
            f"avg={_human_rate(downloaded_total / elapsed)}",
            flush=True,
        )
    finally:
        stop_monitor.set()
        await monitor_task

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
        await client.start(bot_token=settings.bot_token)
        base_session = client.session
        if not base_session.auth_key:
            raise RuntimeError("Main Telegram session has no auth key after bot login")

        print("Telegram bot authorization: ready", flush=True)

        for index in range(TELEGRAM_DOWNLOAD_CONNECTIONS):
            downloader_session = MemorySession()
            downloader_session.set_dc(
                base_session.dc_id,
                base_session.server_address,
                base_session.port,
            )
            downloader_session.auth_key = base_session.auth_key

            downloader = TelegramClient(
                downloader_session,
                settings.telegram_api_id,
                settings.telegram_api_hash,
                connection=ConnectionTcpAbridged,
                receive_updates=False,
                request_retries=5,
                connection_retries=5,
                retry_delay=3,
                auto_reconnect=True,
            )
            await downloader.connect()
            if not downloader.is_connected():
                raise RuntimeError(
                    f"Telegram download connection {index + 1} failed to connect"
                )
            download_clients.append(downloader)
            print(
                f"Telegram download connection {index + 1}/{TELEGRAM_DOWNLOAD_CONNECTIONS} ready "
                f"(dc={downloader.session.dc_id}, transport=abridged)",
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

        me = await client.get_me()
        print(f"Bot started as @{getattr(me, 'username', None) or me.id}", flush=True)
        print(
            f"Telegram downloader: {TELEGRAM_DOWNLOAD_CONNECTIONS} MTProto connections × "
            f"{TELEGRAM_DOWNLOAD_WORKERS} concurrent download workers × "
            f"{TELEGRAM_DOWNLOAD_PART_SIZE_KB} KiB requests × abridged TCP × self-healing × dc={TELEGRAM_DOWNLOAD_DC}",
            flush=True,
        )
        await client.run_until_disconnected()
    finally:
        for downloader in download_clients:
            try:
                await downloader.disconnect()
            except Exception:
                pass
        try:
            await client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
