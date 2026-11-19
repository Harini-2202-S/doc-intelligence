import io
import os
from collections.abc import AsyncIterator, Callable

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pypdf import PdfWriter

from doc_intelligence.config import Settings
from doc_intelligence.db import Base, Database
from doc_intelligence.main import create_app

PASSWORD = "correct horse battery staple"

POLICY_MD = b"""# Acme Leave Policy

Version 2, effective 1 April 2026. Owner: Meera Iyer.

## Annual leave
Every employee earns 18 days of paid annual leave per year. Up to 10 days may be carried forward.

## Sick leave
Employees get 12 days of paid sick leave. A medical certificate is needed after 2 days.

## Expenses
Expense claims must be filed within 30 days. Managers approve claims up to Rs. 25,000.
"""


def make_pdf(pages: int = 1, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    if password:
        writer.encrypt(password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_text_pdf(paragraphs: list[str], hidden: str | None = None) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for text in paragraphs:
        pdf.multi_cell(0, 6, text, new_x="LMARGIN", new_y="NEXT")
    if hidden:
        pdf.set_text_color(255, 255, 255)
        pdf.multi_cell(0, 6, hidden, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,  # never read the developer's .env in tests
        environment="test",
        llm_provider="offline",
        qdrant_location=":memory:",
        # Set TEST_DATABASE_URL to run the suite against PostgreSQL; SQLite is used otherwise.
        database_url=os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        storage_dir=tmp_path / "uploads",
        storage_encryption_key=Fernet.generate_key().decode(),
        queue_backend="inline",
        max_upload_mb=2,
        max_pages=5,
        uploads_per_hour=100,
    )


@pytest.fixture
async def app(settings) -> AsyncIterator[FastAPI]:
    db = Database(settings.database_url)
    async with db.engine.begin() as conn:  # start every test from an empty schema
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, db)
    yield application
    application.state.retriever.store.close()
    await db.dispose()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def register_and_login(client) -> Callable:
    async def _do(email: str = "asha@example.com", password: str = PASSWORD) -> dict[str, str]:
        r = await client.post("/auth/register", json={"email": email, "password": password})
        assert r.status_code == 201, r.text
        r = await client.post("/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    return _do


@pytest.fixture
def collection(client) -> Callable:
    async def _do(headers, name: str | None = None) -> str:
        if name is None:
            return (await client.get("/collections", headers=headers)).json()[0]["id"]
        return (await client.post("/collections", headers=headers, json={"name": name})).json()["id"]

    return _do


@pytest.fixture
def upload(client, collection) -> Callable:
    """Uploads a file; processing runs inline, so the document is ready when this returns."""
    async def _do(headers, data: bytes = POLICY_MD, filename: str = "leave_policy.md",
                  collection_id: str | None = None, title: str | None = None):
        cid = collection_id or await collection(headers)
        form = {"collection_id": cid, **({"title": title} if title else {})}
        return await client.post("/documents", headers=headers, data=form,
                                 files={"file": (filename, data, "application/octet-stream")})

    return _do
