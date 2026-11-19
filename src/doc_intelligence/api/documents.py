"""Document upload, listing, detail (with brief), page text for citations, search and deletion.

Every query filters by the current user's id. Asking for someone else's document returns 404,
not 403, so the API does not reveal that the id exists.
"""

import asyncio
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from doc_intelligence import audit
from doc_intelligence.api.collections import get_owned_collection
from doc_intelligence.config import Settings
from doc_intelligence.deps import get_current_user, get_limiter, get_session, get_settings_dep, get_storage
from doc_intelligence.models import Briefing, Document, DocumentPage, ProcessingJob, User
from doc_intelligence.queue import enqueue_job
from doc_intelligence.rag.embeddings import EmbeddingError
from doc_intelligence.ratelimit import RateLimiter
from doc_intelligence.schemas import (
    BriefingOut,
    DocumentDetailOut,
    DocumentOut,
    JobOut,
    PageOut,
    PassageOut,
    SearchIn,
)
from doc_intelligence.storage import FileStorage
from doc_intelligence.uploads import UploadRejected, safe_display_name, title_from_filename, validate_upload

router = APIRouter(tags=["documents"])


async def get_owned_document(session: AsyncSession, user: User, document_id: uuid.UUID) -> Document:
    doc = (await session.execute(
        select(Document).where(Document.id == document_id, Document.owner_id == user.id)
    )).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


async def _detail(session: AsyncSession, doc: Document) -> DocumentDetailOut:
    job = (await session.execute(
        select(ProcessingJob).where(ProcessingJob.document_id == doc.id, ProcessingJob.owner_id == doc.owner_id)
        .order_by(ProcessingJob.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    brief = (await session.execute(select(Briefing).where(Briefing.document_id == doc.id))).scalar_one_or_none()
    labels = list((await session.execute(
        select(DocumentPage.label).where(DocumentPage.document_id == doc.id).order_by(DocumentPage.number)
    )).scalars())
    return DocumentDetailOut(
        **DocumentOut.model_validate(doc).model_dump(),
        latest_job=JobOut.model_validate(job) if job else None,
        briefing=BriefingOut.model_validate(brief) if brief else None,
        pages=labels,
    )


@router.post("/documents", response_model=DocumentDetailOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    collection_id: uuid.UUID = Form(...),
    title: str | None = Form(default=None, max_length=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    storage: FileStorage = Depends(get_storage),
    limiter: RateLimiter = Depends(get_limiter),
) -> DocumentDetailOut:
    collection = await get_owned_collection(session, user, collection_id)
    if not limiter.allow(f"upload:{user.id}", settings.uploads_per_hour, 3600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Upload limit reached; try again later")
    if not limiter.allow_global("upload", settings.global_uploads_per_hour, 3600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "The service is busy; try again later")

    filename = safe_display_name(file.filename)
    data = await file.read(settings.max_upload_bytes + 1)  # read at most one byte past the limit
    try:
        checked = validate_upload(data, filename, max_bytes=settings.max_upload_bytes, max_pages=settings.max_pages)
    except UploadRejected as exc:
        audit.record(session, "document.upload_rejected", user_id=user.id, request=request,
                     detail={"reason": str(exc)})
        await session.commit()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None

    duplicate = (await session.execute(
        select(Document).where(Document.collection_id == collection.id, Document.sha256 == checked.sha256)
    )).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"message": "This file is already in the collection", "document_id": str(duplicate.id)})

    storage_key = await asyncio.to_thread(storage.put, checked.data)
    try:
        doc = Document(owner_id=user.id, collection_id=collection.id,
                       title=(title or "").strip() or title_from_filename(filename), original_filename=filename,
                       file_format=checked.file_format, media_type=checked.media_type, size_bytes=len(checked.data),
                       sha256=checked.sha256, storage_key=storage_key)
        session.add(doc)
        await session.flush()
        job = ProcessingJob(document_id=doc.id, owner_id=user.id)
        session.add(job)
        audit.record(session, "document.upload", user_id=user.id, target=str(doc.id), request=request,
                     detail={"format": checked.file_format, "bytes": len(checked.data)})
        await session.commit()
    except Exception:
        await asyncio.to_thread(storage.delete, storage_key)  # remove the stored file if the database write fails
        raise

    await enqueue_job(request.app, job.id, background)
    return await _detail(session, doc)


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(collection_id: uuid.UUID | None = None, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)) -> list[Document]:
    query = select(Document).where(Document.owner_id == user.id).order_by(Document.created_at.desc())
    if collection_id:
        query = query.where(Document.collection_id == collection_id)
    return list((await session.execute(query)).scalars())


@router.get("/documents/{document_id}", response_model=DocumentDetailOut)
async def get_document(document_id: uuid.UUID, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)) -> DocumentDetailOut:
    return await _detail(session, await get_owned_document(session, user, document_id))


@router.get("/documents/{document_id}/pages/{number}", response_model=PageOut)
async def get_page(document_id: uuid.UUID, number: int, user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)) -> PageOut:
    """Returns the extracted text of one page, used to display a citation in context."""
    doc = await get_owned_document(session, user, document_id)
    page = (await session.execute(
        select(DocumentPage).where(DocumentPage.document_id == doc.id, DocumentPage.number == number)
    )).scalar_one_or_none()
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    return PageOut(number=page.number, label=page.label, text=page.text, page_count=doc.page_count)


@router.post("/documents/{document_id}/reprocess", response_model=DocumentDetailOut)
async def reprocess_document(document_id: uuid.UUID, request: Request, background: BackgroundTasks,
                             user: User = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)) -> DocumentDetailOut:
    """Runs the processing pipeline again, for example after configuring an API key."""
    doc = await get_owned_document(session, user, document_id)
    current = await _detail(session, doc)
    if current.latest_job and current.latest_job.status in ("queued", "running"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This document is already being processed")
    job = ProcessingJob(document_id=doc.id, owner_id=user.id)
    session.add(job)
    await session.commit()
    await enqueue_job(request.app, job.id, background)
    return await _detail(session, doc)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session),
                          storage: FileStorage = Depends(get_storage)) -> Response:
    """Deletes the file and all derived data: pages, chunks, vectors and brief."""
    doc = await get_owned_document(session, user, document_id)
    request.app.state.retriever.store.delete_document(doc.id)
    await asyncio.to_thread(storage.delete, doc.storage_key)
    audit.record(session, "document.delete", user_id=user.id, target=str(doc.id), request=request)
    await session.delete(doc)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/search", response_model=list[PassageOut])
async def search(body: SearchIn, request: Request, user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)) -> list[PassageOut]:
    """Runs hybrid search over the user's documents and returns the matching passages."""
    if body.collection_id:
        await get_owned_collection(session, user, body.collection_id)
    if body.document_id:
        await get_owned_document(session, user, body.document_id)
    try:
        passages = await request.app.state.retriever.search(
            session, body.query, user.id, [body.collection_id] if body.collection_id else None, body.document_id,
            top_k=8)
    except EmbeddingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    return [PassageOut(**p.citation(), score=round(p.score, 4)) for p in passages]


@router.get("/status")
async def service_status(request: Request, user: User = Depends(get_current_user)) -> dict:
    model = request.app.state.chat_model
    return {
        "embedding_model": request.app.state.embedder.name,
        "chat_model": model.model_name if model else None,
        "indexed_chunks": request.app.state.retriever.store.count(user.id),
    }
