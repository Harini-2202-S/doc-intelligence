"""arq worker. Run with:  arq doc_intelligence.worker.WorkerSettings"""

import uuid

from arq import Retry
from arq.connections import RedisSettings

from doc_intelligence.config import get_settings
from doc_intelligence.db import Database
from doc_intelligence.models import JobStatus
from doc_intelligence.pipeline import run_job
from doc_intelligence.queue import make_chat_model, make_embedder, make_retriever
from doc_intelligence.storage import make_storage


async def startup(ctx: dict) -> None:
    settings = get_settings()
    ctx["settings"] = settings
    ctx["db"] = Database(settings.database_url)
    ctx["storage"] = make_storage(settings)
    ctx["model"] = make_chat_model(settings)
    ctx["retriever"] = make_retriever(settings, make_embedder(settings))


async def shutdown(ctx: dict) -> None:
    ctx["retriever"].store.close()
    await ctx["db"].dispose()


async def process_document(ctx: dict, job_id: str) -> str:
    settings = ctx["settings"]
    status = await run_job(uuid.UUID(job_id), ctx["db"].sessionmaker, ctx["storage"],
                           retriever=ctx["retriever"], model=ctx["model"],
                           max_pages=settings.max_pages, max_rows=settings.max_spreadsheet_rows)
    if status == JobStatus.queued:
        raise Retry(defer=10 * 2 ** (ctx["job_try"] - 1))  # back off: 10s, 20s, 40s
    return status.value


class WorkerSettings:
    functions = [process_document]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = 3
    job_timeout = 600
