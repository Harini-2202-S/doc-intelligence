"""Request and response models for the public API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from doc_intelligence.models import DocumentStatus, JobStatus


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class PasswordConfirmIn(BaseModel):
    """Re-authentication for sensitive operations."""

    password: str = Field(max_length=256)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105  (OAuth token type, not a secret)
    expires_in: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    created_at: datetime


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class CollectionOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    document_count: int = 0
    ready_count: int = 0


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: JobStatus
    current_stage: str | None
    completed_stages: list[str]
    attempts: int
    error: str | None
    updated_at: datetime


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    collection_id: uuid.UUID
    title: str
    original_filename: str
    file_format: str
    size_bytes: int
    status: DocumentStatus
    error: str | None
    page_count: int
    chunk_count: int
    index_version: int
    warnings: list[str]
    created_at: datetime
    indexed_at: datetime | None


class BriefingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    content: dict
    coverage: float
    model: str
    created_at: datetime


class DocumentDetailOut(DocumentOut):
    latest_job: JobOut | None = None
    briefing: BriefingOut | None = None
    pages: list[str] = []  # page labels, in order


class PageOut(BaseModel):
    number: int
    label: str
    text: str
    page_count: int


class SearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    collection_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None


class PassageOut(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    file_format: str
    page: int
    page_label: str
    heading: str | None
    start: int
    end: int
    excerpt: str
    score: float
