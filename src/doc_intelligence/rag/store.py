"""Qdrant vector store.

Runs embedded with a local folder, in memory, or against a Qdrant server URL. Each point is one
chunk; its payload holds the owner, collection and document ids, and every search filters on the
owner.
"""

import uuid

from qdrant_client import QdrantClient, models

FILTER_FIELDS = ("owner", "collection_id", "document_id")


class VectorStore:
    def __init__(self, location: str, embedding_name: str, dimensions: int, api_key: str | None = None) -> None:
        self.remote = location.startswith(("http://", "https://"))
        if self.remote:
            self.client = QdrantClient(url=location, api_key=api_key, timeout=30)
        elif location == ":memory:":
            self.client = QdrantClient(":memory:")
        else:
            self.client = QdrantClient(path=location)
        # One Qdrant collection per embedding model, since vectors from different models are not comparable.
        self.collection = f"chunks_{embedding_name}".replace("/", "_").replace(".", "_")
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(size=dimensions, distance=models.Distance.COSINE),
            )
        if self.remote:
            # A Qdrant server needs payload indexes to filter efficiently (local mode ignores them).
            for field in FILTER_FIELDS:
                self.client.create_payload_index(self.collection, field, models.PayloadSchemaType.KEYWORD)

    def upsert(self, points: list[tuple[uuid.UUID, list[float], dict]]) -> None:
        for start in range(0, len(points), 256):
            self.client.upsert(
                self.collection,
                points=[models.PointStruct(id=str(pid), vector=vec, payload=payload)
                        for pid, vec, payload in points[start:start + 256]],
            )

    def _delete(self, key: str, value: str) -> None:
        self.client.delete(
            self.collection,
            points_selector=models.FilterSelector(filter=models.Filter(must=[
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
            ])),
        )

    def delete_document(self, document_id: uuid.UUID) -> None:
        self._delete("document_id", str(document_id))

    def delete_owner(self, owner_id: uuid.UUID) -> None:
        self._delete("owner", str(owner_id))

    def search(self, vector: list[float], owner_id: uuid.UUID, collection_ids: list[uuid.UUID] | None,
               document_id: uuid.UUID | None, limit: int) -> list[tuple[str, float]]:
        must = [models.FieldCondition(key="owner", match=models.MatchValue(value=str(owner_id)))]
        if collection_ids:
            must.append(models.FieldCondition(key="collection_id",
                                              match=models.MatchAny(any=[str(c) for c in collection_ids])))
        if document_id:
            must.append(models.FieldCondition(key="document_id", match=models.MatchValue(value=str(document_id))))
        result = self.client.query_points(self.collection, query=vector, limit=limit,
                                          query_filter=models.Filter(must=must))
        return [(str(p.id), p.score) for p in result.points]

    def count(self, owner_id: uuid.UUID | None = None) -> int:
        flt = None
        if owner_id is not None:
            flt = models.Filter(must=[models.FieldCondition(key="owner",
                                                            match=models.MatchValue(value=str(owner_id)))])
        return self.client.count(self.collection, count_filter=flt).count

    def close(self) -> None:
        self.client.close()
