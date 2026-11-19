"""Hybrid retrieval: dense (embeddings) + sparse (BM25), merged with Reciprocal Rank Fusion,
then optionally reranked by a cross-encoder.

Access control is applied here, before any text reaches a prompt: only the user's own ready
documents in the requested scope are searched, and dense results from the vector store are
checked against the database.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from doc_intelligence.models import Chunk, Document, DocumentStatus
from doc_intelligence.rag.bm25 import BM25, reciprocal_rank_fusion
from doc_intelligence.rag.embeddings import Embedder
from doc_intelligence.rag.store import VectorStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Passage:
    chunk_id: str
    document_id: str
    title: str
    file_format: str
    page_number: int
    page_label: str
    heading: str | None
    text: str
    start: int
    end: int
    score: float

    def citation(self, excerpt_chars: int = 600) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "title": self.title,
            "file_format": self.file_format,
            "page": self.page_number,
            "page_label": self.page_label,
            "heading": self.heading,
            "start": self.start,
            "end": self.end,
            "excerpt": self.text[:excerpt_chars],
        }


class Reranker(Protocol):
    def rerank(self, query: str, texts: list[str]) -> list[float]: ...


class FastEmbedReranker:
    """Local cross-encoder (ONNX, no GPU). Downloads a ~80 MB model on first use."""

    def __init__(self, model: str = "Xenova/ms-marco-MiniLM-L-6-v2") -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._model = TextCrossEncoder(model_name=model)

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        return list(self._model.rerank(query, texts))


def embedding_text(title: str, heading: str | None, text: str) -> str:
    """Returns the text to embed for a chunk: document title, section heading and chunk text."""
    return f"{title}\n{heading or ''}\n{text}"


class Retriever:
    def __init__(self, store: VectorStore, embedder: Embedder, reranker: Reranker | None = None,
                 candidates: int = 30, top_k: int = 6) -> None:
        self.store, self.embedder, self.reranker = store, embedder, reranker
        self.candidates, self.top_k = candidates, top_k

    async def _accessible(self, session: AsyncSession, owner_id: uuid.UUID, collection_ids: list[uuid.UUID] | None,
                          document_id: uuid.UUID | None) -> dict[str, tuple[Chunk, Document]]:
        query = (
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .where(Document.owner_id == owner_id, Chunk.owner_id == owner_id,
                   Document.status == DocumentStatus.ready)
        )
        if collection_ids:
            query = query.where(Document.collection_id.in_(collection_ids))
        if document_id:
            query = query.where(Document.id == document_id)
        return {str(c.id): (c, d) for c, d in (await session.execute(query)).all()}

    async def search(self, session: AsyncSession, query: str, owner_id: uuid.UUID,
                     collection_ids: list[uuid.UUID] | None = None, document_id: uuid.UUID | None = None,
                     top_k: int | None = None) -> list[Passage]:
        (result,) = await self.search_many(session, [query], owner_id, collection_ids, document_id, top_k)
        return result

    async def search_many(self, session: AsyncSession, queries: list[str], owner_id: uuid.UUID,
                          collection_ids: list[uuid.UUID] | None = None, document_id: uuid.UUID | None = None,
                          top_k: int | None = None) -> list[list[Passage]]:
        """Searches several queries with one database load and one embedding request."""
        chunks = await self._accessible(session, owner_id, collection_ids, document_id)
        if not chunks or not queries:
            return [[] for _ in queries]
        vectors = await self.embedder.embed([q or " " for q in queries])
        bm25 = BM25([(cid, f"{d.title} {c.heading or ''} {c.text}") for cid, (c, d) in chunks.items()])
        results = []
        for query, vector in zip(queries, vectors, strict=True):
            if not query.strip():
                results.append([])
                continue
            dense = [cid for cid, _ in self.store.search(vector, owner_id, collection_ids, document_id,
                                                         self.candidates) if cid in chunks]
            sparse = [cid for cid, _ in bm25.search(query, self.candidates)]
            results.append(await self._fuse(query, dense, sparse, chunks, top_k or self.top_k))
        return results

    async def _fuse(self, query: str, dense: list[str], sparse: list[str],
                    chunks: dict[str, tuple[Chunk, Document]], top_k: int) -> list[Passage]:
        fused = reciprocal_rank_fusion([dense, sparse])[: self.candidates]
        ids = [cid for cid, _ in fused]
        scores = [score for _, score in fused]
        if self.reranker and ids:
            try:
                scores = await asyncio.to_thread(self.reranker.rerank, query, [chunks[cid][0].text for cid in ids])
                order = sorted(range(len(ids)), key=lambda i: scores[i], reverse=True)
                ids, scores = [ids[i] for i in order], [scores[i] for i in order]
            except Exception:  # fall back to the fused order if reranking fails
                log.exception("reranker failed; using fused order")
        return [to_passage(chunks[cid][0], chunks[cid][1], float(score))
                for cid, score in zip(ids[:top_k], scores[:top_k], strict=False)]


def to_passage(chunk: Chunk, doc: Document, score: float = 0.0) -> Passage:
    return Passage(
        chunk_id=str(chunk.id), document_id=str(doc.id), title=doc.title, file_format=doc.file_format,
        page_number=chunk.page_number, page_label=chunk.page_label, heading=chunk.heading,
        text=chunk.text, start=chunk.start, end=chunk.end, score=score,
    )
