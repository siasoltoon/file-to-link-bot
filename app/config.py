from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    owner_telegram_id: int | None = None

    # Kept for backward compatibility with the older Docker/Local Bot API deployment.
    telegram_api_base_url: str = "http://telegram-bot-api:8081/bot"
    telegram_file_base_url: str = "http://telegram-bot-api:8081/file/bot"
    public_base_url: str = ""

    s3_endpoint_url: str
    # Fil.one is already proven to work in the existing YouTube bot with eu-west-1.
    s3_region: str = "eu-west-1"
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_bucket: str

    direct_link_expires_seconds: int = 604800
    max_file_bytes: int = 2_000_000_000
    database_url: str = "sqlite:///./data/app.db"
    app_host: str = "0.0.0.0"
    app_port: int = 8000


settings = Settings()
