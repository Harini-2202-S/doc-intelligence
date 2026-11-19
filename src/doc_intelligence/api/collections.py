"""Collections: workspaces of related documents. Every query filters by the current user."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from doc_intelligence import audit
from doc_intelligence.deps import get_current_user, get_session, get_storage
from doc_intelligence.models import Collection, Document, DocumentStatus, User
from doc_intelligence.schemas import CollectionIn, CollectionOut
from doc_intelligence.storage import FileStorage

router = APIRouter(prefix="/collections", tags=["collections"])
DEFAULT_NAME = "My documents"


async def get_owned_collection(session: AsyncSession, user: User, collection_id: uuid.UUID) -> Collection:
    collection = (await session.execute(
        select(Collection).where(Collection.id == collection_id, Collection.owner_id == user.id)
    )).scalar_one_or_none()
    if collection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Collection not found")
    return collection


async def _with_counts(session: AsyncSession, user: User, collections: list[Collection]) -> list[CollectionOut]:
    rows = (await session.execute(
        select(Document.collection_id, func.count(),
               func.sum(case((Document.status == DocumentStatus.ready, 1), else_=0)))
        .where(Document.owner_id == user.id).group_by(Document.collection_id)
    )).all()
    counts = {cid: (total, ready or 0) for cid, total, ready in rows}
    return [CollectionOut(id=c.id, name=c.name, description=c.description, created_at=c.created_at,
                          document_count=counts.get(c.id, (0, 0))[0], ready_count=counts.get(c.id, (0, 0))[1])
            for c in collections]


@router.get("", response_model=list[CollectionOut])
async def list_collections(user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)) -> list[CollectionOut]:
    collections = list((await session.execute(
        select(Collection).where(Collection.owner_id == user.id).order_by(Collection.created_at)
    )).scalars())
    if not collections:  # create a default collection on first use
        default = Collection(owner_id=user.id, name=DEFAULT_NAME)
        session.add(default)
        await session.commit()
        collections = [default]
    return await _with_counts(session, user, collections)


@router.post("", response_model=CollectionOut, status_code=status.HTTP_201_CREATED)
async def create_collection(body: CollectionIn, user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)) -> CollectionOut:
    collection = Collection(owner_id=user.id, name=body.name.strip(), description=body.description)
    session.add(collection)
    await session.commit()
    return (await _with_counts(session, user, [collection]))[0]


@router.patch("/{collection_id}", response_model=CollectionOut)
async def rename_collection(collection_id: uuid.UUID, body: CollectionIn, user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)) -> CollectionOut:
    collection = await get_owned_collection(session, user, collection_id)
    collection.name, collection.description = body.name.strip(), body.description
    await session.commit()
    return (await _with_counts(session, user, [collection]))[0]


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(collection_id: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session),
                            storage: FileStorage = Depends(get_storage)) -> Response:
    """Deletes the collection with every document's file, vectors, chunks, briefs and chats."""
    collection = await get_owned_collection(session, user, collection_id)
    docs = (await session.execute(select(Document).where(Document.collection_id == collection.id,
                                                         Document.owner_id == user.id))).scalars()
    for doc in docs:
        request.app.state.retriever.store.delete_document(doc.id)
        await asyncio.to_thread(storage.delete, doc.storage_key)
    audit.record(session, "collection.delete", user_id=user.id, target=str(collection.id), request=request)
    await session.delete(collection)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
