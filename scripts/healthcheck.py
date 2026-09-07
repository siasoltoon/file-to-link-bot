from app.config import settings
from app.storage import storage


def require(name: str, value) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RuntimeError(f"Missing required setting: {name}")


require("BOT_TOKEN", settings.bot_token)
require("TELEGRAM_API_ID", settings.telegram_api_id)
require("TELEGRAM_API_HASH", settings.telegram_api_hash)
require("S3_ENDPOINT_URL", settings.s3_endpoint_url)
require("S3_ACCESS_KEY_ID", settings.s3_access_key_id)
require("S3_SECRET_ACCESS_KEY", settings.s3_secret_access_key)
require("S3_BUCKET", settings.s3_bucket)

if not (60 <= settings.direct_link_expires_seconds <= 604800):
    raise RuntimeError("DIRECT_LINK_EXPIRES_SECONDS must be between 60 and 604800")

if settings.max_file_bytes <= 0:
    raise RuntimeError("MAX_FILE_BYTES must be positive")

storage.healthcheck()
print("Configuration and S3 storage healthcheck passed.")
