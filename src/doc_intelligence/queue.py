"""Job dispatch and construction of AI components from settings.

Jobs run in-process in development and tests, or on arq workers backed by Redis in deployment.
"""

import asyncio
import uuid

from fastapi import BackgroundTasks, FastAPI
from sqlalchemy import select

from doc_intelligence.config import Settings
from doc_intelligence.llm import ChatModel, OfflineChat, OpenAIChat
from doc_intelligence.models import JobStatus, ProcessingJob
from doc_intelligence.pipeline import MAX_ATTEMPTS, run_job
from doc_intelligence.rag.embeddings import Embedder, HashEmbedder, OpenAIEmbedder
from doc_intelligence.rag.retriever import FastEmbedReranker, Retriever
from doc_intelligence.rag.store import VectorStore


def _openai_key(settings: Settings) -> str | None:
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    return key or None


def make_embedder(settings: Settings) -> Embedder:
    key = _openai_key(settings)
    if settings.llm_provider == "openai" and key:
        return OpenAIEmbedder(key, settings.embedding_model)
    return HashEmbedder()  # offline / no key: word-overlap vectors


def make_chat_model(settings: Settings) -> ChatModel | None:
    """Returns None when no provider is configured; search still works without a chat model."""
    if settings.llm_provider == "offline":
        return OfflineChat()
    key = _openai_key(settings)
    return OpenAIChat(key, settings.llm_model) if key else None


def make_retriever(settings: Settings, embedder: Embedder) -> Retriever:
    api_key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
    store = VectorStore(settings.qdrant_location, embedder.name, embedder.dimensions, api_key=api_key)
    reranker = FastEmbedReranker() if settings.reranker == "fastembed" else None
    return Retriever(store, embedder, reranker, settings.retrieval_candidates, settings.retrieval_top_k)


async def run_with_retries(app: FastAPI, job_id: uuid.UUID, base_delay_s: float = 2.0) -> JobStatus:
    """Runs a job in-process, retrying transient failures with exponential backoff."""
    status = JobStatus.queued
    settings = app.state.settings
    for attempt in range(MAX_ATTEMPTS):
        status = await run_job(job_id, app.state.db.sessionmaker, app.state.storage,
                               retriever=app.state.retriever, model=app.state.chat_model,
                               max_pages=settings.max_pages, max_rows=settings.max_spreadsheet_rows)
        if status != JobStatus.queued:
            break
        await asyncio.sleep(base_delay_s * 2**attempt)
    return status


async def enqueue_job(app: FastAPI, job_id: uuid.UUID, background: BackgroundTasks) -> None:
    if app.state.settings.queue_backend == "arq":
        # The worker handles retries. Using the job id as the arq job id makes enqueueing idempotent.
        await app.state.arq.enqueue_job("process_document", str(job_id), _job_id=str(job_id))
    else:
        background.add_task(run_with_retries, app, job_id)


async def resume_unfinished_jobs(app: FastAPI) -> int:
    """Re-runs jobs left queued or running by a restart (inline mode only; arq workers retry on their own).

    Jobs are processed one after another so a restart does not trigger a burst of API calls.
    """
    async with app.state.db.sessionmaker() as session:
        job_ids = list((await session.execute(
            select(ProcessingJob.id)
            .where(ProcessingJob.status.in_([JobStatus.queued, JobStatus.running]))
            .order_by(ProcessingJob.created_at)
        )).scalars())
    for job_id in job_ids:
        await run_with_retries(app, job_id)
    return len(job_ids)
