"""Encrypted file storage.

Files are stored under random keys rather than user-supplied names, which rules out path
traversal, and are encrypted with Fernet (AES-128-CBC with HMAC-SHA256) before they leave the
process. Two backends implement the same interface: local disk, and S3-compatible object storage
(AWS S3, Cloudflare R2, MinIO) for deployments without a persistent disk.

The methods are synchronous; async callers run them with `asyncio.to_thread`.
"""

import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from doc_intelligence.config import Settings

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


class FileStorage(Protocol):
    def put(self, data: bytes) -> str: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...


def _fernet(encryption_key: str | None) -> Fernet:
    # Without a configured key (dev and test only; prod refuses to start) an ephemeral key is used.
    return Fernet(encryption_key.encode() if encryption_key else Fernet.generate_key())


def _check_key(key: str) -> str:
    if not _KEY_RE.match(key):
        raise ValueError("invalid storage key")
    return key


class EncryptedFileStorage:
    """Local-disk backend."""

    def __init__(self, root: Path, encryption_key: str | None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._fernet = _fernet(encryption_key)

    def put(self, data: bytes) -> str:
        key = secrets.token_urlsafe(32)
        (self.root / key).write_bytes(self._fernet.encrypt(data))
        return key

    def get(self, key: str) -> bytes:
        return self._fernet.decrypt((self.root / _check_key(key)).read_bytes())

    def delete(self, key: str) -> None:
        (self.root / _check_key(key)).unlink(missing_ok=True)


class S3FileStorage:
    """S3-compatible object storage backend."""

    def __init__(self, bucket: str, access_key_id: str, secret_access_key: str, encryption_key: str | None,
                 endpoint_url: str | None = None, region: str = "auto", prefix: str = "uploads/") -> None:
        import boto3
        from botocore.config import Config

        self.bucket, self.prefix = bucket, prefix
        self._fernet = _fernet(encryption_key)
        self._client = boto3.client(
            "s3", endpoint_url=endpoint_url, region_name=region, aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=10, read_timeout=60),
        )

    def put(self, data: bytes) -> str:
        key = secrets.token_urlsafe(32)
        self._client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=self._fernet.encrypt(data))
        return key

    def get(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            body = self._client.get_object(Bucket=self.bucket, Key=self.prefix + _check_key(key))["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise FileNotFoundError(key) from exc
            raise
        return self._fernet.decrypt(body)

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self.prefix + _check_key(key))


def make_storage(settings: "Settings") -> FileStorage:
    if settings.storage_backend == "s3":
        assert settings.s3_bucket and settings.s3_access_key_id and settings.s3_secret_access_key  # noqa: S101
        return S3FileStorage(
            bucket=settings.s3_bucket, access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key.get_secret_value(),
            encryption_key=settings.storage_encryption_key, endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
        )
    return EncryptedFileStorage(settings.storage_dir, settings.storage_encryption_key)
