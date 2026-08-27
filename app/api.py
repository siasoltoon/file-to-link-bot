import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .config import settings
from .db import delete_record, expired_files, get_by_token, init_db, utcnow
from .storage import storage


async def cleanup_loop() -> None:
    while True:
        try:
            rows = expired_files()
            for row in rows:
                try:
                    await asyncio.to_thread(storage.delete_file, row.object_key)
                    delete_record(row.id)
                except Exception as exc:
                    print(f"cleanup failed for {row.id}: {exc!r}")
        except Exception as exc:
            print(f"cleanup loop error: {exc!r}")
        await asyncio.sleep(max(60, settings.cleanup_interval_seconds))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="File to Link Bot API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/d/{token}")
async def download(token: str):
    if len(token) > 64:
        raise HTTPException(status_code=404, detail="File not found")

    row = get_by_token(token)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")

    if row.expires_at is not None and row.expires_at <= utcnow():
        try:
            await asyncio.to_thread(storage.delete_file, row.object_key)
        finally:
            delete_record(row.id)
        raise HTTPException(status_code=410, detail="Link expired")

    url = await asyncio.to_thread(
        storage.presigned_download_url,
        row.object_key,
        row.filename,
    )
    return RedirectResponse(url=url, status_code=307)
