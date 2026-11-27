from pydantic import model_validator
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

    # Fil.one API keys and buckets are region-scoped. Keep the endpoint derived
    # from the selected region unless an explicit compatible endpoint is supplied.
    s3_region: str = "eu-west-1"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_bucket: str

    direct_link_expires_seconds: int = 604800
    max_file_bytes: int = 2_000_000_000
    database_url: str = "sqlite:///./data/app.db"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    @model_validator(mode="after")
    def configure_filone_endpoint(self):
        endpoints = {
            "eu-west-1": "https://s3.eu-west-1.filonecontent.com",
            "us-east-1": "https://s3.us-east-1.filonecontent.com",
        }
        if self.s3_region not in endpoints:
            raise ValueError(
                "S3_REGION must be eu-west-1 or us-east-1 for Fil.one"
            )
        if not self.s3_endpoint_url:
            self.s3_endpoint_url = endpoints[self.s3_region]
        return self


settings = Settings()
