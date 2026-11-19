"""Registration, login, profile and account deletion."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from doc_intelligence import audit
from doc_intelligence.config import Settings
from doc_intelligence.deps import (
    get_current_user,
    get_limiter,
    get_session,
    get_settings_dep,
    get_storage,
)
from doc_intelligence.models import Collection, Document, User
from doc_intelligence.ratelimit import RateLimiter
from doc_intelligence.schemas import LoginIn, PasswordConfirmIn, RegisterIn, TokenOut, UserOut
from doc_intelligence.security import create_access_token, hash_password, verify_password
from doc_intelligence.storage import FileStorage

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> User:
    if not settings.allow_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Registration is closed")
    if len(body.password) < settings.min_password_length:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Password must be at least {settings.min_password_length} characters",
        )
    user = User(email=body.email.lower(), password_hash=hash_password(body.password))
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered") from None
    audit.record(session, "user.register", user_id=user.id, request=request)
    await session.commit()
    return user


@router.post("/login", response_model=TokenOut)
async def login(
    body: LoginIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    limiter: RateLimiter = Depends(get_limiter),
) -> TokenOut:
    email = body.email.lower()
    if not limiter.allow(f"login:{email}", settings.login_attempts_per_15min, 15 * 60):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts")

    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not verify_password(body.password, user.password_hash if user else None) or user is None:
        audit.record(session, "user.login_failed", request=request, detail={"email": email})
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    audit.record(session, "user.login", user_id=user.id, request=request)
    await session.commit()
    return TokenOut(
        access_token=create_access_token(user.id, settings),
        expires_in=settings.access_token_minutes * 60,
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/delete-account", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    body: PasswordConfirmIn,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    storage: FileStorage = Depends(get_storage),
) -> Response:
    """Deletes the account and all associated data. Requires the current password."""
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password confirmation failed")

    # Files and vectors are stored outside the database and are removed explicitly. Rows are also
    # deleted explicitly so cleanup does not depend on database-level cascades.
    docs = list((await session.execute(select(Document).where(Document.owner_id == user.id))).scalars())
    for doc in docs:
        await asyncio.to_thread(storage.delete, doc.storage_key)
    request.app.state.retriever.store.delete_owner(user.id)
    for collection in (await session.execute(select(Collection).where(Collection.owner_id == user.id))).scalars():
        await session.delete(collection)
    audit.record(session, "user.delete", user_id=user.id, request=request, detail={"documents": len(docs)})
    await session.delete(user)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
