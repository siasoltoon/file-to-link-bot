# File to Link Bot

A production-oriented Telegram bot that receives files, stores them in S3-compatible object storage, and returns stable direct-download links.

## Current architecture

```text
Telegram
   |
   | Local Bot API (up to 2 GB target)
   v
Ubuntu VM
   +-- Telegram Local Bot API
   +-- Python Telegram bot
   +-- FastAPI link API
   +-- SQLite metadata
   +-- automatic cleanup
   |
   v
S3-compatible object storage
   |
   v
Stable URL: /d/<token>
   |
   v
Short-lived signed storage URL -> redirect
```

The stable URL belongs to the application. Each request generates a short-lived S3-compatible presigned URL and redirects the downloader to object storage, so the application does not proxy every download.

## File lifecycle

1. User sends a document, video, audio file, or animation.
2. The Local Bot API downloads/stages the file on the VM.
3. The bot uploads the staged file directly to S3-compatible storage.
4. Metadata and the random public token are saved in SQLite.
5. The bot returns `PUBLIC_BASE_URL/d/<token>`.
6. The staged Telegram file is deleted from the VM after processing.
7. The public link remains stable while its metadata is valid.
8. Expired non-owner records are removed from object storage and SQLite by the cleanup worker.

The configured owner gets a non-expiring link. Other users default to `DEFAULT_FILE_TTL_DAYS`.

## Important bandwidth behavior

The VM is used for the Telegram-to-object-storage upload path. A downloader is redirected to object storage instead of downloading through the VM. This is important when the VM has limited monthly bandwidth.

For a 2 GB upload, the VM can consume roughly 4 GB of transfer when Telegram delivers the file to the VM and the VM uploads it to object storage. Actual accounting depends on the providers and network path.

## Local development

Requirements:

- Docker + Docker Compose
- Telegram `api_id` and `api_hash` from `my.telegram.org`
- A Telegram bot token from BotFather
- An S3-compatible bucket and credentials

1. Copy `.env.example` to `.env`.
2. Fill all required values.
3. Set `PUBLIC_BASE_URL` to the address users can reach for the FastAPI service.
4. Start the stack:

```bash
docker compose up -d --build
```

5. Check the API:

```text
GET /health
```

The web API listens on port `8000` by default. Port `8081` of the Local Bot API is intentionally kept private inside Docker.

## Production deployment

The intended deployment is a Linux VM. Put HTTPS reverse proxying (Nginx, Caddy, or an equivalent) in front of port 8000 and set `PUBLIC_BASE_URL` to the resulting HTTPS origin.

Do not expose the Local Bot API port publicly.

Before moving an existing bot from Telegram's hosted Bot API to the Local Bot API, log out the existing Bot API session as required by Telegram's Local Bot API behavior.

## Configuration

See `.env.example` for all settings. The important defaults are:

- `MAX_FILE_SIZE_MB=2000`
- `DEFAULT_FILE_TTL_DAYS=30`
- `PRESIGNED_URL_TTL_SECONDS=300`
- `MAX_CONCURRENT_UPLOADS=1`
- SQLite at `data/app.db`

`MAX_CONCURRENT_UPLOADS=1` is deliberate for small VMs: it prevents multiple multi-gigabyte transfers from exhausting disk space and I/O. Increase it only after testing available storage and network capacity.

## Security

- Never commit `.env`, bot tokens, Telegram API credentials, or S3 secrets.
- Keep port 8081 private.
- Use HTTPS for public links in production.
- Public tokens are generated with Python's cryptographically secure `secrets` module.
- Expiry is checked before a signed storage URL is generated.
- Expired objects are deleted from storage and then removed from metadata.
- Uploaded files are deleted from the Local Bot API staging area after successful or failed processing when possible.

## Repository status

This repository is maintained as a deployment-ready baseline. The current branch contains the hardened upload, link, storage, cleanup, configuration, and Docker workflow. Secrets and provider-specific credentials remain outside Git.
