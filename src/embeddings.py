"""
Embedding logic for the RAG pipeline.
Supports OpenAI and Azure OpenAI endpoints.
Model: text-embedding-3-small (1536 dimensions, fast and cheap)
"""

import asyncio
import os
from typing import Optional

import openai

# ── Client factory ─────────────────────────────────────────────────────────────

def _get_client() -> openai.AsyncOpenAI:
    """
    Returns an async OpenAI client.
    Set AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY to use Azure OpenAI.
    Otherwise falls back to standard OpenAI with OPENAI_API_KEY.
    """
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")

    if azure_endpoint and azure_api_key:
        return openai.AsyncAzureOpenAI(
            azure_endpoint=azure_endpoint,
            api_key=azure_api_key,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "No embedding credentials found. Set OPENAI_API_KEY or "
            "AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY."
        )
    return openai.AsyncOpenAI(api_key=api_key)


_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
_DIMENSIONS = 1536


# ── Public API ─────────────────────────────────────────────────────────────────

async def embed_text(text: str) -> list[float]:
    """Embed a single text string. Returns a 1536-dim vector."""
    client = _get_client()
    response = await client.embeddings.create(
        model=_MODEL,
        input=text.strip(),
    )
    return response.data[0].embedding


async def embed_batch(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """
    Embed a list of texts in batches.
    OpenAI allows up to 2048 inputs per request; we use 64 to stay well within limits.
    """
    client = _get_client()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = [t.strip() for t in texts[i : i + batch_size]]
        response = await client.embeddings.create(model=_MODEL, input=batch)
        # Results are ordered by index
        batch_embeddings = [r.embedding for r in sorted(response.data, key=lambda x: x.index)]
        all_embeddings.extend(batch_embeddings)

    return all_embeddings


def get_dimensions() -> int:
    return _DIMENSIONS
