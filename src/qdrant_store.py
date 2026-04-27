import os, uuid
from typing import Optional
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue, ScoredPoint
from embeddings import get_dimensions

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "infra-knowledge")

def _get_client():
    return AsyncQdrantClient(url=QDRANT_URL)

async def ensure_collection():
    client = _get_client()
    existing = await client.get_collections()
    if COLLECTION_NAME not in [c.name for c in existing.collections]:
        await client.create_collection(collection_name=COLLECTION_NAME, vectors_config=VectorParams(size=get_dimensions(), distance=Distance.COSINE))

async def upsert_chunks(chunks, embeddings):
    client = _get_client()
    await ensure_collection()
    points = [PointStruct(id=str(uuid.uuid4()), vector=emb, payload={"text": c["text"], "source": c.get("source",""), "doc_type": c.get("doc_type","docs"), **c.get("metadata",{})}) for c, emb in zip(chunks, embeddings)]
    await client.upsert(collection_name=COLLECTION_NAME, points=points)
    return len(points)

async def search(query_vector, top_k=5, doc_type=None, source_filter=None):
    client = _get_client()
    conditions = []
    if doc_type: conditions.append(FieldCondition(key="doc_type", match=MatchValue(value=doc_type)))
    if source_filter: conditions.append(FieldCondition(key="source", match=MatchValue(value=source_filter)))
    f = Filter(must=conditions) if conditions else None
    results = await client.search(collection_name=COLLECTION_NAME, query_vector=query_vector, limit=top_k, query_filter=f, with_payload=True, score_threshold=None)
    return [{"text": r.payload.get("text",""), "source": r.payload.get("source",""), "doc_type": r.payload.get("doc_type",""), "score": r.score, "metadata": {k:v for k,v in r.payload.items() if k not in ("text","source","doc_type")}} for r in results]

async def get_collection_info():
    client = _get_client()
    try:
        info = await client.get_collection(COLLECTION_NAME)
        return {"collection": COLLECTION_NAME, "points_count": info.points_count, "status": str(info.status)}
    except Exception as e:
        return {"collection": COLLECTION_NAME, "error": str(e)}