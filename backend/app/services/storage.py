"""Object storage on DO Spaces (S3-compatible, boto3); MinIO in local dev.

The bucket is private. Objects are content-addressed under
``<kind>/<yyyy>/<sha256>.<ext>`` (DESIGN §4 "Files") and the browser only ever
gets a short-lived presigned GET URL (≤ 10 min) after an auth check.

Presigned URLs point at the storage host itself (path style,
``https://nyc3.digitaloceanspaces.com/n16chat/<key>?X-Amz-…``). Used as
``<img src>``, ``<audio src>`` or a download link they need no CORS, and the
SPA sends no Content-Security-Policy today; a future CSP must allow that host
in ``img-src``/``media-src`` (and ``connect-src`` if the SPA ever ``fetch``es
them, which would also need a CORS rule on the bucket).
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from functools import lru_cache
from urllib.parse import quote

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import Settings, get_settings

MAX_URL_TTL_SECONDS = 600
# boto3's default (10 threads x 8 MB parts) would buffer ~80 MB per upload; the
# worker runs in 256 MB, so keep managed uploads to two 8 MB parts in flight.
UPLOAD_TRANSFER = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
    max_concurrency=2,
)


class StorageNotConfigured(RuntimeError):
    pass


def storage_key(kind: str, sha256: str, ext: str, when: datetime | None = None) -> str:
    year = (when or datetime.now(UTC)).year
    return f"{kind}/{year:04d}/{sha256}.{ext}"


def _client(settings: Settings, endpoint: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=settings.do_spaces_region,
        aws_access_key_id=settings.do_spaces_key,
        aws_secret_access_key=settings.do_spaces_secret,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


class Storage:
    def __init__(self, settings: Settings):
        if not settings.storage_configured:
            raise StorageNotConfigured("DO_SPACES_KEY / DO_SPACES_SECRET are not set")
        self.bucket = settings.do_spaces_bucket
        self.url_ttl = max(1, min(settings.attachment_url_ttl_seconds, MAX_URL_TTL_SECONDS))
        self._s3 = _client(settings, settings.do_spaces_endpoint)
        public = settings.do_spaces_public_endpoint or settings.do_spaces_endpoint
        # Signing needs no network call; a second client only changes the host
        # the URL (and its signature) is built for.
        self._signer = (
            self._s3 if public == settings.do_spaces_endpoint else _client(settings, public)
        )

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ACL="private",
        )

    def upload_file(self, key: str, path: str, content_type: str) -> None:
        """Upload a local file (boto3's managed transfer: multipart when large)."""
        self._s3.upload_file(
            path,
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type, "ACL": "private"},
            Config=UPLOAD_TRANSFER,
        )

    def iter_object(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        """Stream an object's bytes (never the whole object in memory)."""
        body = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            yield from body.iter_chunks(chunk_size)
        finally:
            body.close()

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=key)

    # Multipart uploads: a large file sent by the browser in chunks, each one
    # stored as a part the moment it arrives (no API-side state between
    # requests). Parts are 5 MB–5 GB except the last; numbers are 1–10000.

    def create_multipart(self, key: str, content_type: str) -> str:
        response = self._s3.create_multipart_upload(
            Bucket=self.bucket, Key=key, ContentType=content_type, ACL="private"
        )
        return response["UploadId"]

    def upload_part(self, key: str, upload_id: str, part_number: int, data: bytes) -> str:
        response = self._s3.upload_part(
            Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=part_number, Body=data
        )
        return response["ETag"]

    def list_parts(self, key: str, upload_id: str) -> list[dict]:
        """Every uploaded part as ``{"PartNumber", "Size", "ETag"}`` (all pages)."""
        parts: list[dict] = []
        marker = 0
        while True:
            response = self._s3.list_parts(
                Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumberMarker=marker
            )
            parts.extend(
                {"PartNumber": p["PartNumber"], "Size": p["Size"], "ETag": p["ETag"]}
                for p in response.get("Parts", [])
            )
            if not response.get("IsTruncated"):
                return parts
            marker = response["NextPartNumberMarker"]

    def complete_multipart(self, key: str, upload_id: str, parts: list[dict]) -> None:
        self._s3.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in parts]
            },
        )

    def abort_multipart(self, key: str, upload_id: str) -> None:
        self._s3.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)

    def presigned_get(
        self, key: str, *, filename: str, content_type: str, download: bool = False
    ) -> str:
        disposition = "attachment" if download else "inline"
        return self._signer.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": (
                    f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}"
                ),
            },
            ExpiresIn=self.url_ttl,
        )


@lru_cache
def get_storage() -> Storage:
    """FastAPI dependency (overridden in tests). Raises when not configured
    (not cached then, so setting the keys and restarting is enough)."""
    return Storage(get_settings())
