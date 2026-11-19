"""Application settings, loaded from environment variables (prefix DI_) or a .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEV_SECRET = "dev-only-change-me-dev-only-change-me"  # noqa: S105

# libpq connection options that asyncpg does not accept as keyword arguments.
_LIBPQ_ONLY_PARAMS = {"channel_binding", "sslmode", "options", "target_session_attrs"}


def normalize_database_url(url: str) -> str:
    """Converts provider connection strings (postgres://, postgresql://?sslmode=...) to the
    SQLAlchemy asyncpg form. SQLite and already-qualified URLs are returned unchanged."""
    scheme, _, rest = url.partition("://")
    if scheme not in ("postgres", "postgresql"):
        return url
    parts = urlsplit(f"postgresql+asyncpg://{rest}")
    query = dict(parse_qsl(parts.query))
    sslmode = query.get("sslmode")
    query = {k: v for k, v in query.items() if k not in _LIBPQ_ONLY_PARAMS}
    if sslmode in ("require", "verify-ca", "verify-full") and "ssl" not in query:
        query["ssl"] = "require"
    return urlunsplit(parts._replace(query=urlencode(query)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DI_", env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"

    # Database: PostgreSQL in deployment, SQLite for local development.
    database_url: str = "sqlite+aiosqlite:///./doc_intelligence.db"

    # Auth
    jwt_secret: str = INSECURE_DEV_SECRET
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60
    min_password_length: int = 12

    # File storage. Files are encrypted with the Fernet key before they are written to either backend.
    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: Path = Path("./data/uploads")
    storage_encryption_key: str | None = None
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None  # e.g. https://<account-id>.r2.cloudflarestorage.com for Cloudflare R2
    s3_region: str = "auto"
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None

    # Upload limits
    max_upload_mb: int = 25
    max_pages: int = 300  # PDF pages, slides or sheets
    max_spreadsheet_rows: int = 5000

    # Abuse limits per user, over a rolling hour
    uploads_per_hour: int = 60
    login_attempts_per_15min: int = 10
    chat_messages_per_hour: int = 120
    # Limits across all users, over a rolling hour (0 disables). Caps API spend on a public deployment.
    global_uploads_per_hour: int = 0
    global_chat_messages_per_hour: int = 0
    allow_registration: bool = True

    # Jobs: "inline" runs processing in-process; "arq" uses Redis workers.
    queue_backend: Literal["inline", "arq"] = "inline"
    redis_url: str = "redis://localhost:6379/0"

    # LLM provider. "offline" runs without an API key using extractive answers.
    llm_provider: Literal["openai", "offline"] = "openai"
    openai_api_key: SecretStr | None = None
    llm_model: str = "gpt-4.1-mini"

    # RAG
    embedding_model: str = "text-embedding-3-small"
    qdrant_location: str = "./data/qdrant"  # local folder, ":memory:", or a server URL (https://...)
    qdrant_api_key: SecretStr | None = None
    retrieval_candidates: int = 30
    retrieval_top_k: int = 6
    reranker: Literal["none", "fastembed"] = "none"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.storage_encryption_key:
            from cryptography.fernet import Fernet

            try:
                Fernet(self.storage_encryption_key.encode())
            except (ValueError, TypeError) as exc:
                raise ValueError("DI_STORAGE_ENCRYPTION_KEY is not a valid Fernet key") from exc
        s3_ready = self.s3_bucket and self.s3_access_key_id and self.s3_secret_access_key
        if self.storage_backend == "s3" and not s3_ready:
            raise ValueError("DI_STORAGE_BACKEND=s3 requires DI_S3_BUCKET, DI_S3_ACCESS_KEY_ID and "
                             "DI_S3_SECRET_ACCESS_KEY")
        if self.environment == "prod":
            if self.jwt_secret == INSECURE_DEV_SECRET or len(self.jwt_secret) < 32:
                raise ValueError("DI_JWT_SECRET must be set to a random value of 32+ chars in prod")
            if not self.storage_encryption_key:
                raise ValueError("DI_STORAGE_ENCRYPTION_KEY must be set in prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
