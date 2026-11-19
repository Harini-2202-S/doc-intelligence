"""Ask: chat sessions over a collection or a single document."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from doc_intelligence.api.collections import get_owned_collection
from doc_intelligence.api.documents import get_owned_document
from doc_intelligence.chat import answer_question, new_session
from doc_intelligence.config import Settings
from doc_intelligence.deps import get_current_user, get_limiter, get_session, get_settings_dep
from doc_intelligence.llm import ChatModelError
from doc_intelligence.models import ChatMessage, ChatSession, Collection, Document, User
from doc_intelligence.rag.embeddings import EmbeddingError
from doc_intelligence.ratelimit import RateLimiter

router = APIRouter(prefix="/chat", tags=["chat"])


class SessionIn(BaseModel):
    collection_id: uuid.UUID
    document_id: uuid.UUID | None = None


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    citations: list[dict]
    grounded: bool | None
    search_query: str | None
    created_at: datetime


class SessionOut(BaseModel):
    id: uuid.UUID
    title: str
    collection_id: uuid.UUID
    collection_name: str | None = None
    document_id: uuid.UUID | None
    document_title: str | None = None
    updated_at: datetime
    messages: list[MessageOut] = []


async def _owned_session(session: AsyncSession, user: User, session_id: uuid.UUID) -> ChatSession:
    chat = (await session.execute(
        select(ChatSession).options(selectinload(ChatSession.messages))
        .where(ChatSession.id == session_id, ChatSession.owner_id == user.id)
    )).scalar_one_or_none()
    if chat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return chat


def _message_out(m: ChatMessage) -> MessageOut:
    return MessageOut(id=m.id, role=m.role, content=m.content, citations=m.citations or [], grounded=m.grounded,
                      search_query=m.search_query, created_at=m.created_at)


async def _session_out(session: AsyncSession, chat: ChatSession, with_messages: bool) -> SessionOut:
    collection = await session.get(Collection, chat.collection_id)
    doc = await session.get(Document, chat.document_id) if chat.document_id else None
    return SessionOut(id=chat.id, title=chat.title, collection_id=chat.collection_id,
                      collection_name=collection.name if collection else None, document_id=chat.document_id,
                      document_title=doc.title if doc else None, updated_at=chat.updated_at,
                      messages=[_message_out(m) for m in chat.messages] if with_messages else [])


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(collection_id: uuid.UUID | None = None, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)) -> list[SessionOut]:
    query = select(ChatSession).where(ChatSession.owner_id == user.id).order_by(ChatSession.updated_at.desc())
    if collection_id:
        query = query.where(ChatSession.collection_id == collection_id)
    return [await _session_out(session, c, with_messages=False) for c in (await session.execute(query)).scalars()]


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionIn, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)) -> SessionOut:
    await get_owned_collection(session, user, body.collection_id)
    if body.document_id is not None:
        doc = await get_owned_document(session, user, body.document_id)
        if doc.collection_id != body.collection_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Document is not in that collection")
    chat = new_session(user.id, body.collection_id, body.document_id)
    session.add(chat)
    await session.commit()
    await session.refresh(chat, ["messages"])
    return await _session_out(session, chat, with_messages=False)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session_detail(session_id: uuid.UUID, user: User = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)) -> SessionOut:
    return await _session_out(session, await _owned_session(session, user, session_id), with_messages=True)


@router.post("/sessions/{session_id}/messages", response_model=MessageOut)
async def ask(session_id: uuid.UUID, body: MessageIn, request: Request,
              user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session),
              settings: Settings = Depends(get_settings_dep),
              limiter: RateLimiter = Depends(get_limiter)) -> MessageOut:
    if not limiter.allow(f"chat:{user.id}", settings.chat_messages_per_hour, 3600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Message limit reached; try again later")
    if not limiter.allow_global("chat", settings.global_chat_messages_per_hour, 3600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "The service is busy; try again later")
    model = request.app.state.chat_model
    if model is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "No AI provider configured: set DI_OPENAI_API_KEY in .env and restart")
    chat = await _owned_session(session, user, session_id)
    try:
        reply = await answer_question(session, chat, body.content.strip(), model, request.app.state.retriever)
    except (ChatModelError, EmbeddingError) as exc:
        code = status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, str(exc)) from None
    return _message_out(reply)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: uuid.UUID, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)) -> Response:
    await session.delete(await _owned_session(session, user, session_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
