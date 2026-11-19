"""Deployment configuration: hosted database URLs, S3-compatible storage, settings validation."""

import boto3
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from moto import mock_aws
from pydantic import ValidationError

from doc_intelligence.config import Settings, normalize_database_url
from doc_intelligence.db import Base, Database
from doc_intelligence.main import create_app
from doc_intelligence.storage import S3FileStorage, make_storage
from tests.conftest import PASSWORD, POLICY_MD

KEY = Fernet.generate_key().decode()


@pytest.mark.parametrize(("raw", "expected"), [
    ("postgres://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
    ("postgresql://u:p@ep-x.neon.tech/db?sslmode=require&channel_binding=require",
     "postgresql+asyncpg://u:p@ep-x.neon.tech/db?ssl=require"),
    ("postgresql+asyncpg://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
    ("sqlite+aiosqlite:///./local.db", "sqlite+aiosqlite:///./local.db"),
])
def test_provider_database_urls_are_normalized(raw, expected):
    assert normalize_database_url(raw) == expected
    assert Settings(_env_file=None, database_url=raw).database_url == expected


def test_invalid_settings_are_rejected():
    with pytest.raises(ValidationError, match="Fernet"):
        Settings(_env_file=None, storage_encryption_key="not-a-key")
    with pytest.raises(ValidationError, match="DI_S3_BUCKET"):
        Settings(_env_file=None, storage_backend="s3")
    with pytest.raises(ValidationError, match="DI_JWT_SECRET"):
        Settings(_env_file=None, environment="prod", storage_encryption_key=KEY)


def _s3_settings(tmp_path, **extra) -> Settings:
    return Settings(_env_file=None, environment="test", llm_provider="offline", qdrant_location=":memory:",
                    database_url=f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}",
                    storage_backend="s3", s3_bucket="docs", s3_access_key_id="test", s3_secret_access_key="test",
                    s3_region="us-east-1", storage_encryption_key=KEY, **extra)


@mock_aws
def test_s3_storage_round_trip_is_encrypted(tmp_path):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="docs")
    storage = make_storage(_s3_settings(tmp_path))
    assert isinstance(storage, S3FileStorage)
    key = storage.put(b"confidential policy text")
    raw = boto3.client("s3", region_name="us-east-1").get_object(Bucket="docs", Key=f"uploads/{key}")["Body"].read()
    assert b"confidential" not in raw  # stored encrypted
    assert storage.get(key) == b"confidential policy text"
    storage.delete(key)
    with pytest.raises(FileNotFoundError):
        storage.get(key)
    with pytest.raises(ValueError, match="invalid storage key"):
        storage.get("../../etc/passwd")


@pytest.fixture
def s3():
    with mock_aws():
        yield


async def test_upload_process_and_delete_with_s3_backend(tmp_path, s3):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="docs")
    settings = _s3_settings(tmp_path)
    db = Database(settings.database_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(settings, db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/register", json={"email": "s3@example.com", "password": PASSWORD})
        token = (await client.post("/auth/login", json={"email": "s3@example.com", "password": PASSWORD})).json()
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        col = (await client.get("/collections", headers=headers)).json()[0]["id"]
        r = await client.post("/documents", headers=headers, data={"collection_id": col},
                              files={"file": ("policy.md", POLICY_MD, "text/markdown")})
        assert r.status_code == 201
        doc = (await client.get(f"/documents/{r.json()['id']}", headers=headers)).json()
        assert doc["status"] == "ready" and doc["briefing"]
        objects = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="docs").get("KeyCount")
        assert objects == 1
        assert (await client.delete(f"/documents/{doc['id']}", headers=headers)).status_code == 204
        assert boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="docs").get("KeyCount") == 0
    app.state.retriever.store.close()
    await db.dispose()


async def test_global_caps_and_closed_registration(client, register_and_login, upload, app, settings):
    headers = await register_and_login()
    settings.global_uploads_per_hour = 1
    assert (await upload(headers)).status_code == 201
    r = await upload(headers, b"# Other\nA different document with enough text to index.", "other.md")
    assert r.status_code == 429 and "busy" in r.json()["detail"]

    settings.global_chat_messages_per_hour = 1
    col = (await client.get("/collections", headers=headers)).json()[0]["id"]
    chat = (await client.post("/chat/sessions", headers=headers, json={"collection_id": col})).json()
    url = f"/chat/sessions/{chat['id']}/messages"
    assert (await client.post(url, headers=headers, json={"content": "annual leave"})).status_code == 200
    assert (await client.post(url, headers=headers, json={"content": "sick leave"})).status_code == 429

    settings.allow_registration = False
    r = await client.post("/auth/register", json={"email": "late@example.com", "password": PASSWORD})
    assert r.status_code == 403


def test_qdrant_server_mode_uses_api_key_and_payload_indexes(monkeypatch):
    from doc_intelligence.rag import store as store_module

    calls: dict = {"indexes": []}

    class FakeClient:
        def __init__(self, url=None, api_key=None, timeout=None, **_):
            calls["url"], calls["api_key"] = url, api_key

        def collection_exists(self, name):
            return False

        def create_collection(self, name, vectors_config):
            calls["collection"] = name

        def create_payload_index(self, name, field, schema):
            calls["indexes"].append(field)

    monkeypatch.setattr(store_module, "QdrantClient", FakeClient)
    store_module.VectorStore("https://cluster.qdrant.example:6333", "openai-text-embedding-3-small", 1536,
                             api_key="secret")
    assert calls["api_key"] == "secret" and calls["url"].startswith("https://")
    assert calls["indexes"] == ["owner", "collection_id", "document_id"]
