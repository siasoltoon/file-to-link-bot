from urllib.parse import quote

import boto3
from botocore.config import Config

from .config import settings


class Storage:
    def __init__(self) -> None:
        # Keep Fil.one client settings aligned with the already-working YouTube bot.
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
        )

    def upload_file(self, local_path: str, object_key: str, content_type: str | None = None) -> None:
        extra_args = {"ContentType": content_type} if content_type else None
        self.client.upload_file(local_path, settings.s3_bucket, object_key, ExtraArgs=extra_args)

    def delete_file(self, object_key: str) -> None:
        self.client.delete_object(Bucket=settings.s3_bucket, Key=object_key)

    def presigned_download_url(self, object_key: str, filename: str, expires_seconds: int | None = None) -> str:
        expires = expires_seconds or settings.direct_link_expires_seconds
        expires = max(60, min(int(expires), 604800))
        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
        disposition = f"attachment; filename*=UTF-8''{quote(safe_name)}"
        url = self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.s3_bucket,
                "Key": object_key,
                "ResponseContentDisposition": disposition,
            },
            ExpiresIn=expires,
        )
        if not url:
            raise RuntimeError("Fil.one presigned URL generation failed")
        return url

    def healthcheck(self) -> None:
        # Client construction validates the local configuration shape. Avoid HeadBucket/ListBucket
        # because S3-compatible providers may deny those permissions even when upload/download works.
        if not settings.s3_endpoint_url or not settings.s3_bucket:
            raise RuntimeError("S3 endpoint or bucket is missing")


storage = Storage()
