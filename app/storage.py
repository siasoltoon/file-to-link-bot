from pathlib import Path
from urllib.parse import quote

import boto3
from boto3.s3.transfer import TransferConfig
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
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 8, "mode": "standard"},
            ),
        )

        # Large files use multipart uploads with parallel requests.  This keeps the
        # normal small-file path simple while making 400MB+ uploads much more robust.
        self.transfer_config = TransferConfig(
            multipart_threshold=16 * 1024 * 1024,
            multipart_chunksize=16 * 1024 * 1024,
            max_concurrency=8,
            num_download_attempts=8,
            use_threads=True,
        )

    def upload_file(
        self,
        local_path: str,
        object_key: str,
        content_type: str | None = None,
        callback=None,
    ) -> None:
        filename = (
            Path(local_path)
            .name.replace('"', "")
            .replace("\r", "")
            .replace("\n", "")
        )
        extra_args = {
            "ContentType": content_type or "application/octet-stream",
            "ContentDisposition": f'attachment; filename="{filename}"',
        }
        self.client.upload_file(
            local_path,
            settings.s3_bucket,
            object_key,
            ExtraArgs=extra_args,
            Callback=callback,
            Config=self.transfer_config,
        )

    def delete_file(self, object_key: str) -> None:
        self.client.delete_object(Bucket=settings.s3_bucket, Key=object_key)

    def presigned_download_url(
        self,
        object_key: str,
        filename: str,
        expires_seconds: int | None = None,
        content_type: str | None = None,
    ) -> str:
        expires = expires_seconds or settings.direct_link_expires_seconds
        expires = max(60, min(int(expires), 604800))

        # Override the download response headers on the signed GET so the browser
        # uses the original Telegram filename instead of an S3/object-key name.
        safe_filename = filename.replace("\r", "").replace("\n", "").replace('"', "")
        ascii_fallback = Path(safe_filename).name.encode("ascii", "ignore").decode("ascii").strip()
        ascii_fallback = ascii_fallback or "download"
        encoded_filename = quote(safe_filename, safe="")
        disposition = (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{encoded_filename}"
        )

        params = {
            "Bucket": settings.s3_bucket,
            "Key": object_key,
            "ResponseContentDisposition": disposition,
        }
        if content_type:
            params["ResponseContentType"] = content_type

        url = self.client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires,
        )
        if not url:
            raise RuntimeError("Fil.one presigned URL generation failed")
        return url

    def healthcheck(self) -> None:
        # Avoid HeadBucket/ListBucket because compatible providers may deny those permissions
        # even when upload and signed downloads work correctly.
        if not settings.s3_endpoint_url or not settings.s3_bucket:
            raise RuntimeError("S3 endpoint or bucket is missing")


storage = Storage()
