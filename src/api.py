"""
FastAPI application for the RAG pipeline.
Exposes query, ingest, and health endpoints.

Endpoints:
  GET  /health              — liveness probe
  GET  /ready               — readiness probe (checks Qdrant connection)
  GET  /info                — collection stats
  POST /query               — semantic search
  POST /ingest/text         — ingest raw text
  POST /ingest/directory    — ingest a local directory (server-side path)
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from embeddings import embed_text
from qdrant_store import search, get_collection_info, ensure_collection
from ingest import ingest_directory, ingest_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensure Qdrant collection exists on startup."""
    logger.info("Starting RAG pipeline API...")
    try:
        await ensure_collection()
        logger.info("Qdrant collection ready")
    except Exception as e:
        logger.warning(f"Could not connect to Qdrant on startup: {e}")
    yield
    logger.info("RAG pipeline API shutting down")


app = FastAPI(
    title="AWS/Azure RAG Pipeline",
    description="Retrieval-augmented generation for infrastructure docs and code",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ──────────────────────────────────────────────────

class QueryRequest(BaseModel):
    query: str = Field(..., description="Natural language query")
    top_k: int = Field(5, ge=1, le=20, description="Number of results to return")
    doc_type: Optional[str] = Field(None, description="Filter by type: docs, code, runbook")
    source_filter: Optional[str] = Field(None, description="Filter by source file path")


class QueryResult(BaseModel):
    text: str
    source: str
    doc_type: str
    score: float
    metadata: dict


class QueryResponse(BaseModel):
    query: str
    results: list[QueryResult]
    elapsed_seconds: float
    total_results: int


class IngestTextRequest(BaseModel):
    text: str = Field(..., description="Raw text to ingest")
    source: str = Field(..., description="Source identifier (e.g. file path or URL)")
    doc_type: str = Field("docs", description="Type: docs, code, or runbook")
    metadata: dict = Field(default_factory=dict, description="Optional metadata")


class IngestDirectoryRequest(BaseModel):
    directory: str = Field(..., description="Server-side directory path to ingest")
    repo_name: str = Field("", description="Repository name tag")


# ── Health endpoints ───────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "rag-pipeline"}


@app.get("/ready")
async def ready():
    """Readiness probe — checks Qdrant is reachable."""
    try:
        info = await get_collection_info()
        if "error" in info:
            raise HTTPException(status_code=503, detail=f"Qdrant not ready: {info['error']}")
        return {"status": "ready", "qdrant": "connected"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/info")
async def info():
    """Return collection statistics."""
    return await get_collection_info()


# ── Query endpoint ─────────────────────────────────────────────────────────────

@app.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest):
    """
    Semantic search over ingested infrastructure docs and code.
    Returns the most relevant chunks for the given query.
    """
    start = time.monotonic()

    try:
        query_vector = await embed_text(request.query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {str(e)}")

    try:
        results = await search(
            query_vector=query_vector,
            top_k=request.top_k,
            doc_type=request.doc_type,
            source_filter=request.source_filter,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

    elapsed = round(time.monotonic() - start, 3)
    logger.info(f"Query '{request.query[:50]}...' returned {len(results)} results in {elapsed}s")

    return QueryResponse(
        query=request.query,
        results=[QueryResult(**r) for r in results],
        elapsed_seconds=elapsed,
        total_results=len(results),
    )


# ── Ingest endpoints ───────────────────────────────────────────────────────────

@app.post("/ingest/text")
async def ingest_text_endpoint(request: IngestTextRequest):
    """Ingest a raw text string into the knowledge base."""
    try:
        result = await ingest_text(
            text=request.text,
            source=request.source,
            doc_type=request.doc_type,
            metadata=request.metadata,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ingest/directory")
async def ingest_directory_endpoint(
    request: IngestDirectoryRequest,
    background_tasks: BackgroundTasks,
):
    """
    Ingest all docs and code from a server-side directory.
    Runs in the background — returns immediately with a job ID.
    """
    job_id = f"ingest-{int(time.time())}"

    async def _run():
        result = await ingest_directory(
            directory=request.directory,
            repo_name=request.repo_name,
        )
        logger.info(f"Ingest job {job_id} complete: {result}")

    background_tasks.add_task(_run)
    return {
        "status": "accepted",
        "job_id": job_id,
        "directory": request.directory,
        "message": "Ingestion started in background. Check /info for progress.",
    }


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("ENV", "production") == "development",
    )
