"""Audit logging of security-relevant actions. Document content is never recorded."""

import uuid

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from doc_intelligence.models import AuditLog


def client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def record(
    session: AsyncSession,
    action: str,
    *,
    user_id: uuid.UUID | None = None,
    target: str | None = None,
    request: Request | None = None,
    detail: dict | None = None,
) -> None:
    """Adds an audit entry to the session; it is committed with the caller's transaction."""
    session.add(
        AuditLog(
            user_id=user_id,
            action=action,
            target=target,
            ip=client_ip(request),
            detail=detail,
        )
    )
