# File to Link Bot

Telegram bot that receives files (target: up to 2,000 MB via Telegram Local Bot API), stores them in S3-compatible object storage (initial provider: Fil One), and returns stable direct-download links.

## Target deployment

- **Oracle Cloud Always Free**: Telegram Local Bot API + bot + download-link API
- **Fil One**: persistent object storage
- **SQLite initially**: file metadata and link lifecycle; can be moved to PostgreSQL later

Oracle's current Always Free A1 allocation is 2 OCPUs + 12 GB RAM total, with 200 GB combined boot/block storage and 10 TB/month outbound data transfer. The application is intentionally designed to stay within those limits.

Telegram's Local Bot API supports unlimited file downloads and uploads up to 2000 MB. The production deployment therefore uses a self-hosted Local Bot API on the Oracle VM.

## Link policy

- **All users**: stable links with no expiration and unlimited downloads. Files are retained until manually deleted; no automatic TTL cleanup is applied.
- No download-count limits.

## Architecture

```text
Telegram
   |
   v
Oracle Always Free VM
   +-- Local Telegram Bot API (2 GB support)
   +-- Python Bot
   +-- Download API
   +-- Cleanup worker
   +-- SQLite metadata
   |
   v
Fil One (S3-compatible object storage)
   |
   v
Stable link: /d/<token>
```

The stable link is owned by our API. For each request the API creates a short-lived Fil One presigned URL and redirects the browser to it. This lets owner links remain stable even though the underlying storage URL is temporary.

## Repository status

This repository currently contains the initial production-oriented scaffold. Secrets are never committed; use `.env.example` as the template.

## First deployment steps

1. Create an Oracle Cloud Always Free A1 VM in the home region.
2. Install Docker and Docker Compose.
3. Obtain Telegram `api_id` and `api_hash` from `my.telegram.org`.
4. Create the Telegram bot with BotFather and obtain its token.
5. Create a Fil One bucket and an API key in the **same region** as that bucket. The default configuration is `eu-west-1` (France); set `S3_REGION=us-east-1` only if your bucket and API key were created in US East (Michigan). The endpoint is derived automatically from this value. Fil.one requires S3 path-style addressing and Signature Version 4.
6. Fill `.env` from `.env.example`, including `S3_BUCKET`, `S3_ACCESS_KEY_ID`, and `S3_SECRET_ACCESS_KEY`. Set `PUBLIC_BASE_URL` to the public HTTPS base URL of this app (for example, `https://files.example.com`) to send stable `/d/<token>` links. This setting is required for the permanent-link behavior; without it, the bot falls back to a temporary Fil.one URL.
7. Start the Local Bot API and application with Docker Compose.
8. Before switching an existing bot from Telegram's hosted Bot API, call `logOut` on the old Bot API session as required by Telegram's Local Bot API documentation.

## Security rules

- Never commit `.env` or tokens.
- Keep the Telegram Local Bot API port private; expose only the application/download API through HTTPS.
- Use a reverse proxy with TLS in production.
- Generate cryptographically random public link tokens.
- Link tokens do not expire. Each visit generates a fresh short-lived Fil.one download URL and redirects to it, so the temporary storage URL is never the permanent public link.
- Files are not automatically deleted. Add a manual deletion/admin workflow before enabling any retention policy.
