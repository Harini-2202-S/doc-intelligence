"""Document processing: an ordered list of stages run as a resumable job.

    verify_file -> parse -> index -> brief

Each stage commits before the next one starts, so a crash or retry resumes from the last
completed stage without repeating embedding or LLM calls. Stages are idempotent. A document is
searchable once `index` completes; the brief is generated afterwards.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_intelligence.briefing import PROMPT_VERSION, BriefingError, generate_briefing
from doc_intelligence.llm import ChatModel
from doc_intelligence.models import Briefing, Chunk, Document, DocumentPage, DocumentStatus, JobStatus, ProcessingJob
from doc_intelligence.parsing import ParseError, parse_document
from doc_intelligence.rag.chunking import chunk_pages
from doc_intelligence.rag.embeddings import EmbeddingError
from doc_intelligence.rag.retriever import Retriever, embedding_text, to_passage
from doc_intelligence.storage import FileStorage
from doc_intelligence.uploads import UploadRejected, detect_format

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class StageError(Exception):
    """A stage failed. `retryable` separates transient failures from permanent ones."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class StageContext:
    session: AsyncSession
    job: ProcessingJob
    document: Document
    storage: FileStorage
    retriever: Retriever | None = None
    model: ChatModel | None = None
    max_pages: int = 300
    max_rows: int = 5000


Stage = Callable[[StageContext], Awaitable[None]]


async def stage_verify_file(ctx: StageContext) -> None:
    """Checks that the stored file exists, decrypts, and still matches the recorded format."""
    try:
        data = await asyncio.to_thread(ctx.storage.get, ctx.document.storage_key)
    except FileNotFoundError as exc:
        raise StageError("Stored file is missing", retryable=False) from exc
    try:
        fmt = detect_format(data, ctx.document.original_filename)
    except UploadRejected as exc:
        raise StageError(str(exc), retryable=False) from exc
    if fmt != ctx.document.file_format:
        raise StageError("Stored file type does not match the upload", retryable=False)


async def stage_parse(ctx: StageContext) -> None:
    doc = ctx.document
    try:
        data = await asyncio.to_thread(ctx.storage.get, doc.storage_key)
        parsed = await asyncio.to_thread(parse_document, data, doc.file_format,
                                         max_pages=ctx.max_pages, max_rows=ctx.max_rows)
    except ParseError as exc:
        raise StageError(str(exc), retryable=False) from exc
    await ctx.session.execute(delete(DocumentPage).where(DocumentPage.document_id == doc.id))
    ctx.session.add_all(DocumentPage(document_id=doc.id, number=i, label=p.label, text=p.text)
                        for i, p in enumerate(parsed.pages, start=1))
    doc.page_count = len(parsed.pages)
    doc.char_count = parsed.char_count
    doc.warnings = parsed.warnings


async def stage_index(ctx: StageContext) -> None:
    if ctx.retriever is None:
        raise StageError("Search index is not configured", retryable=False)
    doc = ctx.document
    pages = list((await ctx.session.execute(
        select(DocumentPage).where(DocumentPage.document_id == doc.id).order_by(DocumentPage.number)
    )).scalars())
    pieces = chunk_pages([p.text for p in pages])
    if not pieces:
        raise StageError("No indexable text found", retryable=False)
    try:
        vectors = await ctx.retriever.embedder.embed([embedding_text(doc.title, c.heading, c.text) for c in pieces])
    except EmbeddingError as exc:
        raise StageError(f"Embedding failed: {exc}", retryable=exc.retryable) from exc

    # Replace any earlier index of this document (re-processing bumps index_version).
    ctx.retriever.store.delete_document(doc.id)
    await ctx.session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    rows = [Chunk(id=uuid.uuid4(), document_id=doc.id, owner_id=doc.owner_id, collection_id=doc.collection_id,
                  position=c.position, page_number=c.page_number, page_label=pages[c.page_number - 1].label,
                  heading=c.heading, text=c.text, start=c.start, end=c.end) for c in pieces]
    ctx.session.add_all(rows)
    await ctx.session.flush()
    ctx.retriever.store.upsert([
        (row.id, vec, {"owner": str(doc.owner_id), "collection_id": str(doc.collection_id),
                       "document_id": str(doc.id)})
        for row, vec in zip(rows, vectors, strict=True)
    ])
    doc.chunk_count = len(rows)
    doc.embedding_model = ctx.retriever.embedder.name
    doc.index_version += 1
    doc.indexed_at = datetime.now(UTC)
    doc.status = DocumentStatus.ready
    doc.error = None


async def stage_brief(ctx: StageContext) -> None:
    if ctx.model is None:
        raise StageError("No AI provider configured: set DI_OPENAI_API_KEY in .env for briefings "
                         "(search still works)", retryable=False)
    doc = ctx.document
    chunks = list((await ctx.session.execute(
        select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.position)
    )).scalars())
    try:
        result = await generate_briefing(ctx.model, doc.title, [to_passage(c, doc) for c in chunks])
    except BriefingError as exc:
        raise StageError(str(exc), retryable=exc.retryable) from exc
    await ctx.session.execute(delete(Briefing).where(Briefing.document_id == doc.id))
    ctx.session.add(Briefing(document_id=doc.id, owner_id=doc.owner_id, content=result.content,
                             coverage=result.coverage, model=result.model, prompt_version=PROMPT_VERSION))


STAGES: list[tuple[str, Stage]] = [
    ("verify_file", stage_verify_file),
    ("parse", stage_parse),
    ("index", stage_index),
    ("brief", stage_brief),
]


async def run_job(
    job_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: FileStorage,
    stages: list[tuple[str, Stage]] | None = None,
    retriever: Retriever | None = None,
    model: ChatModel | None = None,
    max_pages: int = 300,
    max_rows: int = 5000,
) -> JobStatus:
    """Runs (or resumes) a processing job and returns its final status for this attempt."""
    stages = stages if stages is not None else STAGES
    async with sessionmaker() as session:
        job = await session.get(ProcessingJob, job_id)
        if job is None:
            log.warning("job %s not found (document deleted?)", job_id)
            return JobStatus.failed
        if job.status == JobStatus.completed:
            return job.status
        document = await session.get(Document, job.document_id)
        if document is None:
            job.status, job.error = JobStatus.failed, "Document no longer exists"
            await session.commit()
            return job.status

        job.status = JobStatus.running
        job.attempts += 1
        job.error = None
        await session.commit()

        ctx = StageContext(session=session, job=job, document=document, storage=storage, retriever=retriever,
                           model=model, max_pages=max_pages, max_rows=max_rows)
        for name, stage in stages:
            if name in job.completed_stages:
                continue  # done in an earlier attempt
            job.current_stage = name
            await session.commit()
            try:
                await stage(ctx)
            except StageError as exc:
                await session.rollback()
                await session.refresh(job)
                await session.refresh(document)
                return await _fail(session, job, document, name, str(exc), exc.retryable)
            except Exception as exc:  # unexpected error: record it so the job is not left running
                log.exception("stage %s crashed (job %s)", name, job.id)
                await session.rollback()
                await session.refresh(job)
                await session.refresh(document)
                return await _fail(session, job, document, name, f"Internal error: {type(exc).__name__}", True)
            job.completed_stages = [*job.completed_stages, name]
            await session.commit()

        job.status = JobStatus.completed
        job.current_stage = None
        await session.commit()
        return job.status


async def _fail(session: AsyncSession, job: ProcessingJob, document: Document, stage: str, message: str,
                retryable: bool) -> JobStatus:
    can_retry = retryable and job.attempts < MAX_ATTEMPTS
    job.status = JobStatus.queued if can_retry else JobStatus.failed
    job.error = f"{stage}: {message}"
    # A failed brief leaves the document searchable; a failed parse or index marks it failed.
    if not can_retry and document.status != DocumentStatus.ready:
        document.status = DocumentStatus.failed
        document.error = message[:300]
    await session.commit()
    return job.status
