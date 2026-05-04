"""
FastAPI application for the RAG pipeline.
Exposes query, ingest, and health endpoints.

Endpoints:
  GET  /health              — liveness probe
  GET  /ready               — readiness probe (checks Qdrant connection)
  GET  /info                — collection stats
  POST /query               — semantic search
  POST /ask                 — full RAG: retrieve + generate answer
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
from llm_provider import generate_answer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensure Qdrant collection exists on startup and auto-ingest if empty."""
    logger.info("Starting RAG pipeline API...")
    try:
        await ensure_collection()
        logger.info("Qdrant collection ready")

        # Auto-ingest on startup if collection is empty
        if os.getenv("INGEST_ON_STARTUP", "false").lower() == "true":
            info = await get_collection_info()
            points = info.get("points_count", 0)

            if points == 0:
                logger.info("Collection is empty — starting auto-ingest...")
                ingest_dirs = os.getenv("INGEST_DIRS", "")
                if ingest_dirs:
                    dirs = [d.strip() for d in ingest_dirs.split(",") if d.strip()]
                    for directory in dirs:
                        logger.info(f"Auto-ingesting: {directory}")
                        asyncio.create_task(_auto_ingest(directory))
                else:
                    logger.warning("INGEST_ON_STARTUP=true but INGEST_DIRS is not set")
            else:
                logger.info(f"Collection already has {points} points — skipping auto-ingest")

    except Exception as e:
        logger.warning(f"Could not connect to Qdrant on startup: {e}")
    yield
    logger.info("RAG pipeline API shutting down")


async def _auto_ingest(directory: str):
    """Background task to ingest a directory on startup."""
    try:
        repo_name = directory.rstrip("/").split("/")[-1]
        logger.info(f"Auto-ingest started: {repo_name}")
        result = await ingest_directory(directory=directory, repo_name=repo_name)
        logger.info(f"Auto-ingest complete: {repo_name} — {result.get('chunks_ingested', 0)} chunks")
    except Exception as e:
        logger.error(f"Auto-ingest failed for {directory}: {e}")


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


class AskRequest(BaseModel):
    query: str = Field(..., description="Natural language question")
    top_k: int = Field(5, ge=1, le=20, description="Number of chunks to retrieve")
    doc_type: Optional[str] = Field(None, description="Filter by type: docs, code, runbook")
    provider: Optional[str] = Field(None, description="LLM provider: claude or openai")


class AskResponse(BaseModel):
    query: str
    answer: str
    provider: str
    model: str
    sources: list[QueryResult]
    elapsed_seconds: float

class CompareResponse(BaseModel):
    query: str
    claude: dict
    openai: dict
    elapsed_seconds: float

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


# ── Ask endpoint (full RAG) ────────────────────────────────────────────────────

@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest):
    """
    Full RAG — retrieves relevant chunks then generates a grounded answer.
    Set provider='claude' (default) or provider='openai' per request,
    or globally via LLM_PROVIDER environment variable.
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
            source_filter=None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

    try:
        llm_response = await generate_answer(
            query=request.query,
            context_chunks=results,
            provider=request.provider,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM generation failed: {str(e)}")

    elapsed = round(time.monotonic() - start, 3)
    logger.info(
        f"/ask '{request.query[:50]}' → {llm_response['provider']} "
        f"({len(results)} chunks) in {elapsed}s"
    )

    return AskResponse(
        query=request.query,
        answer=llm_response["answer"],
        provider=llm_response["provider"],
        model=llm_response["model"],
        sources=[QueryResult(**r) for r in results],
        elapsed_seconds=elapsed,
    )

@app.post("/compare", response_model=CompareResponse)
async def compare(request: AskRequest):
    """
    Run the same query against both Claude and OpenAI simultaneously.
    Returns both answers side by side for comparison.
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
            source_filter=None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

    # Run both providers in parallel
    claude_task = asyncio.create_task(
        generate_answer(query=request.query, context_chunks=results, provider="claude")
    )
    openai_task = asyncio.create_task(
        generate_answer(query=request.query, context_chunks=results, provider="openai")
    )

    claude_result, openai_result = await asyncio.gather(
        claude_task, openai_task, return_exceptions=True
    )

    elapsed = round(time.monotonic() - start, 3)

    def _format(result, provider_name):
        if isinstance(result, Exception):
            return {"error": str(result), "provider": provider_name, "answer": None}
        return result

    return CompareResponse(
        query=request.query,
        claude=_format(claude_result, "claude"),
        openai=_format(openai_result, "openai"),
        elapsed_seconds=elapsed,
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