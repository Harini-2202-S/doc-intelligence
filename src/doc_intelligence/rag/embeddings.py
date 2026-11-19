"""Text embeddings behind one interface.

OpenAIEmbedder calls the OpenAI embeddings API. HashEmbedder is a deterministic bag-of-words
substitute for tests and offline mode; it captures word overlap, not meaning.
"""

import hashlib
import math
import re
from typing import Protocol

_WORD = re.compile(r"[a-z0-9]+")


class EmbeddingError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Embedder(Protocol):
    name: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str = "text-embedding-3-small", dimensions: int = 1536) -> None:
        from openai import AsyncOpenAI

        self.name = f"openai-{model}"
        self.model = model
        self.dimensions = dimensions
        self._client = AsyncOpenAI(api_key=api_key, timeout=60, max_retries=2)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import openai

        vectors: list[list[float]] = []
        for start in range(0, len(texts), 100):  # batch requests to stay within API limits
            batch = [t[:8000] or " " for t in texts[start:start + 100]]
            try:
                response = await self._client.embeddings.create(model=self.model, input=batch)
            except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
                raise EmbeddingError("OpenAI rejected the API key", retryable=False) from exc
            except openai.NotFoundError as exc:
                raise EmbeddingError(f"Embedding model '{self.model}' not available", retryable=False) from exc
            except openai.OpenAIError as exc:
                raise EmbeddingError(f"Embedding request failed: {type(exc).__name__}", retryable=True) from exc
            vectors.extend(item.embedding for item in response.data)
        return vectors


class HashEmbedder:
    name = "hash-bow-256"
    dimensions = 256

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for word in _WORD.findall(text.lower()):
            digest = hashlib.blake2b(word.encode(), digest_size=4).digest()
            vec[int.from_bytes(digest, "little") % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
