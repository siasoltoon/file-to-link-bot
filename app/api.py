import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .db import delete_record, expired_files, get_by_token, init_db, utcnow
from .storage import storage


async def cleanup_loop() -> None:
    while True:
        try:
            for row in expired_files():
                try:
                    await asyncio.to_thread(storage.delete_file, row.object_key)
                    delete_record(row.id)
                except Exception as exc:
                    print(f"cleanup failed for {row.id}: {exc!r}")
        except Exception as exc:
            print(f"cleanup loop error: {exc!r}")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="File to Link Bot", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/d/{token}")
async def download(token: str):
    row = get_by_token(token)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")

    if row.expires_at is not None and row.expires_at <= utcnow():
        try:
            await asyncio.to_thread(storage.delete_file, row.object_key)
        finally:
            delete_record(row.id)
        raise HTTPException(status_code=410, detail="Link expired")

    url = await asyncio.to_thread(storage.presigned_download_url, row.object_key, row.filename, 300)
    return RedirectResponse(url=url, status_code=307)
