"""FastAPI application factory. Run with:  uvicorn doc_intelligence.main:create_app --factory --reload"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from doc_intelligence.api import auth, collections, documents
from doc_intelligence.api import chat as chat_api
from doc_intelligence.config import Settings, get_settings
from doc_intelligence.db import Database
from doc_intelligence.queue import make_chat_model, make_embedder, make_retriever, resume_unfinished_jobs
from doc_intelligence.ratelimit import RateLimiter
from doc_intelligence.storage import make_storage

log = logging.getLogger("doc_intelligence.http")
STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.environment != "prod" and settings.database_url.startswith("sqlite"):
            await app.state.db.create_all()  # dev convenience; prod runs Alembic migrations
        if settings.queue_backend == "arq":
            from arq import create_pool
            from arq.connections import RedisSettings

            app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        else:
            app.state.resume_task = asyncio.create_task(resume_unfinished_jobs(app))
        yield
        if settings.queue_backend == "arq":
            await app.state.arq.aclose()
        else:
            app.state.resume_task.cancel()
        app.state.retriever.store.close()
        await app.state.db.dispose()

    app = FastAPI(
        title="Doc Intelligence API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.environment == "prod" else "/docs",
    )
    app.state.settings = settings
    app.state.db = db or Database(settings.database_url)
    app.state.storage = make_storage(settings)
    app.state.limiter = RateLimiter()
    app.state.embedder = make_embedder(settings)
    app.state.retriever = make_retriever(settings, app.state.embedder)
    app.state.chat_model = make_chat_model(settings)
    if app.state.chat_model is None:
        log.warning("No AI provider configured: answers and briefs need DI_OPENAI_API_KEY (search still works)")

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        if request.url.path == "/" or request.url.path.startswith("/static"):
            # The UI loads only its own static files: no inline scripts or third-party content.
            response.headers["content-security-policy"] = (
                "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
            )
            # Force revalidation so cached assets always match the current index.html.
            response.headers["cache-control"] = "no-cache"
        log.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        async with app.state.db.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def ui() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth.router)
    app.include_router(collections.router)
    app.include_router(documents.router)
    app.include_router(chat_api.router)
    return app
