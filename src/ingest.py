"""
Ingestion pipeline for the RAG system.
Handles markdown docs, runbooks, and code files.

Chunking strategy:
  - Docs/runbooks: split by heading then by token count (512 tokens, 64 overlap)
  - Code: split by top-level function/class then by line count (100 lines, 20 overlap)
"""

import asyncio
import os
import re
from pathlib import Path
from typing import Iterator

from embeddings import embed_batch
from qdrant_store import upsert_chunks, ensure_collection

# ── Constants ──────────────────────────────────────────────────────────────────

CHUNK_SIZE = 512        # approximate tokens (~4 chars/token)
CHUNK_OVERLAP = 64
CODE_CHUNK_LINES = 100
CODE_OVERLAP_LINES = 20

DOC_EXTENSIONS = {".md", ".txt", ".rst"}
CODE_EXTENSIONS = {".py", ".ts", ".js", ".go", ".tf", ".hcl", ".yaml", ".yml", ".json"}

# ── Text chunking ──────────────────────────────────────────────────────────────

def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks by approximate token count."""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk_words = words[i : i + chunk_size]
        chunks.append(" ".join(chunk_words))
        i += chunk_size - overlap
    return [c for c in chunks if c.strip()]


def _chunk_by_heading(text: str) -> list[str]:
    """Split markdown by headings first, then chunk each section."""
    sections = re.split(r"\n(?=#{1,3} )", text)
    chunks = []
    for section in sections:
        if len(section.split()) <= CHUNK_SIZE:
            if section.strip():
                chunks.append(section.strip())
        else:
            chunks.extend(_chunk_text(section))
    return chunks


def _chunk_code(text: str) -> list[str]:
    """Split code by top-level definitions, then by line count."""
    # Try to split at top-level function/class definitions
    splits = re.split(r"\n(?=(?:def |class |async def |func |function ))", text)

    chunks = []
    for split in splits:
        lines = split.splitlines()
        if len(lines) <= CODE_CHUNK_LINES:
            if split.strip():
                chunks.append(split.strip())
        else:
            # Further chunk by line count with overlap
            i = 0
            while i < len(lines):
                chunk = "\n".join(lines[i : i + CODE_CHUNK_LINES])
                if chunk.strip():
                    chunks.append(chunk)
                i += CODE_CHUNK_LINES - CODE_OVERLAP_LINES
    return chunks


# ── File ingestion ─────────────────────────────────────────────────────────────

def _ingest_file(path: Path, repo_name: str = "") -> list[dict]:
    """Read a file and return a list of chunk dicts ready for embedding."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  ⚠ Could not read {path}: {e}")
        return []

    suffix = path.suffix.lower()
    rel_path = str(path)

    if suffix in DOC_EXTENSIONS:
        doc_type = "runbook" if "runbook" in str(path).lower() else "docs"
        raw_chunks = _chunk_by_heading(text)
    elif suffix in CODE_EXTENSIONS:
        doc_type = "code"
        raw_chunks = _chunk_code(text)
    else:
        return []

    return [
        {
            "text": chunk,
            "source": rel_path,
            "doc_type": doc_type,
            "metadata": {
                "repo": repo_name,
                "filename": path.name,
                "extension": suffix,
            },
        }
        for chunk in raw_chunks
        if len(chunk.strip()) > 50  # skip trivially short chunks
    ]


# ── Directory ingestion ────────────────────────────────────────────────────────

def _collect_files(directory: Path, repo_name: str = "") -> list[dict]:
    """Walk a directory and collect all ingestible chunks."""
    all_chunks = []
    skip_dirs = {".git", "__pycache__", "node_modules", ".terraform", ".venv", "venv"}

    for path in directory.rglob("*"):
        if any(skip in path.parts for skip in skip_dirs):
            continue
        if path.is_file():
            chunks = _ingest_file(path, repo_name=repo_name or directory.name)
            all_chunks.extend(chunks)

    return all_chunks


# ── Main ingest function ───────────────────────────────────────────────────────

async def ingest_directory(
    directory: str,
    repo_name: str = "",
    batch_size: int = 64,
) -> dict:
    """
    Ingest all docs and code from a directory into Qdrant.

    Args:
        directory: Path to the directory to ingest
        repo_name: Optional repo name tag for metadata
        batch_size: Number of chunks to embed at once

    Returns:
        Summary dict with counts and any errors
    """
    path = Path(directory)
    if not path.exists():
        return {"error": f"Directory not found: {directory}"}

    print(f"📂 Collecting files from {path}...")
    chunks = _collect_files(path, repo_name=repo_name or path.name)

    if not chunks:
        return {"error": "No ingestible files found", "directory": directory}

    print(f"📄 Found {len(chunks)} chunks across {len(set(c['source'] for c in chunks))} files")

    await ensure_collection()

    total_upserted = 0
    errors = []

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        texts = [c["text"] for c in batch]
        try:
            print(f"  🔢 Embedding batch {i // batch_size + 1}/{(len(chunks) - 1) // batch_size + 1}...")
            embeddings = await embed_batch(texts)
            upserted = await upsert_chunks(batch, embeddings)
            total_upserted += upserted
        except Exception as e:
            errors.append(f"Batch {i}-{i+batch_size}: {str(e)}")
            print(f"  ⚠ Batch error: {e}")

    print(f"✅ Ingested {total_upserted} chunks")
    return {
        "status": "complete",
        "directory": directory,
        "repo_name": repo_name,
        "chunks_ingested": total_upserted,
        "files_processed": len(set(c["source"] for c in chunks)),
        "errors": errors,
    }


async def ingest_text(
    text: str,
    source: str,
    doc_type: str = "docs",
    metadata: dict = None,
) -> dict:
    """Ingest a raw text string directly (useful for API-driven ingestion)."""
    if doc_type == "code":
        raw_chunks = _chunk_code(text)
    else:
        raw_chunks = _chunk_by_heading(text)

    chunks = [
        {
            "text": chunk,
            "source": source,
            "doc_type": doc_type,
            "metadata": metadata or {},
        }
        for chunk in raw_chunks
        if len(chunk.strip()) > 50
    ]

    if not chunks:
        return {"error": "No usable chunks extracted from text"}

    embeddings = await embed_batch([c["text"] for c in chunks])
    upserted = await upsert_chunks(chunks, embeddings)

    return {
        "status": "complete",
        "source": source,
        "chunks_ingested": upserted,
    }


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python ingest.py <directory> [repo_name]")
        sys.exit(1)

    directory = sys.argv[1]
    repo_name = sys.argv[2] if len(sys.argv) > 2 else ""

    result = asyncio.run(ingest_directory(directory, repo_name=repo_name))
    print(json.dumps(result, indent=2))
