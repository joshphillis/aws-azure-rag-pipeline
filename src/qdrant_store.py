"""
Qdrant vector database client wrapper.
Handles collection setup, upsert, and similarity search.
"""

import os
import uuid
from typing import Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    SearchRequest,
)

from embeddings import get_dimensions

# ── Config ─────────────────────────────────────────────────────────────────────

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "infra-knowledge")


def _get_client() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=QDRANT_URL)


# ── Collection management ──────────────────────────────────────────────────────

async def ensure_collection() -> None:
    """Create the collection if it doesn't exist."""
    client = _get_client()
    existing = await client.get_collections()
    names = [c.name for c in existing.collections]

    if COLLECTION_NAME not in names:
        await client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=get_dimensions(),
                distance=Distance.COSINE,
            ),
        )


# ── Upsert ─────────────────────────────────────────────────────────────────────

async def upsert_chunks(chunks: list[dict], embeddings: list[list[float]]) -> int:
    """
    Upsert document chunks with their embeddings into Qdrant.

    Each chunk dict should contain:
      - text: str           — the chunk content
      - source: str         — file path or URL
      - doc_type: str       — 'docs' | 'code' | 'runbook'
      - metadata: dict      — any additional fields (repo, language, etc.)
    """
    client = _get_client()
    await ensure_collection()

    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=embedding,
            payload={
                "text": chunk["text"],
                "source": chunk.get("source", "unknown"),
                "doc_type": chunk.get("doc_type", "docs"),
                **chunk.get("metadata", {}),
            },
        )
        for chunk, embedding in zip(chunks, embeddings)
    ]

    await client.upsert(collection_name=COLLECTION_NAME, points=points)
    return len(points)


# ── Search ─────────────────────────────────────────────────────────────────────

async def search(
    query_vector: list[float],
    top_k: int = 5,
    doc_type: Optional[str] = None,
    source_filter: Optional[str] = None,
) -> list[dict]:
    """
    Search for similar chunks in Qdrant.
    Optionally filter by doc_type ('docs', 'code', 'runbook') or source path.
    """
    client = _get_client()

    # Build optional filters
    conditions = []
    if doc_type:
        conditions.append(FieldCondition(key="doc_type", match=MatchValue(value=doc_type)))
    if source_filter:
        conditions.append(FieldCondition(key="source", match=MatchValue(value=source_filter)))

    search_filter = Filter(must=conditions) if conditions else None

    results = await client.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_vector,
        limit=top_k,
        query_filter=search_filter,
        with_payload=True,
    )

    return [
        {
            "text": r.payload.get("text", ""),
            "source": r.payload.get("source", ""),
            "doc_type": r.payload.get("doc_type", ""),
            "score": r.score,
            "metadata": {k: v for k, v in r.payload.items() if k not in ("text", "source", "doc_type")},
        }
        for r in results
    ]


async def get_collection_info() -> dict:
    """Return collection stats."""
    client = _get_client()
    try:
        info = await client.get_collection(COLLECTION_NAME)
        return {
            "collection": COLLECTION_NAME,
            "vectors_count": info.vectors_count,
            "points_count": info.points_count,
            "status": str(info.status),
        }
    except Exception as e:
        return {"collection": COLLECTION_NAME, "error": str(e)}
