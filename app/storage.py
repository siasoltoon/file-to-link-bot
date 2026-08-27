from pathlib import Path

import boto3
from botocore.config import Config

from .config import settings


class Storage:
    def __init__(self) -> None:
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
                connect_timeout=30,
                read_timeout=120,
            ),
        )

    def upload_file(self, local_path: str, object_key: str, content_type: str | None = None) -> None:
        extra_args = {"ContentType": content_type} if content_type else None
        self.client.upload_file(
            local_path,
            settings.s3_bucket,
            object_key,
            ExtraArgs=extra_args,
        )

    def delete_file(self, object_key: str) -> None:
        self.client.delete_object(Bucket=settings.s3_bucket, Key=object_key)

    def presigned_download_url(
        self,
        object_key: str,
        filename: str,
        expires_seconds: int | None = None,
    ) -> str:
        safe_name = Path(filename).name.replace('"', "") or "file"
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.s3_bucket,
                "Key": object_key,
                "ResponseContentDisposition": f'attachment; filename="{safe_name}"',
            },
            ExpiresIn=expires_seconds or settings.presigned_url_ttl_seconds,
        )


storage = Storage()
