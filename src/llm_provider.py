"""
LLM provider abstraction for the RAG pipeline.
Supports Anthropic Claude and OpenAI GPT as swappable answer-generation backends.

Set LLM_PROVIDER=claude  (default) to use Anthropic Claude.
Set LLM_PROVIDER=openai          to use OpenAI GPT.
"""

import os
from typing import Optional

SYSTEM_PROMPT = """You are an expert infrastructure assistant with deep knowledge of 
AWS and Azure cloud environments. You answer questions using only the context provided 
from the infrastructure knowledge base. If the context does not contain enough 
information to answer the question, say so clearly. Never make up infrastructure 
details, IP addresses, resource names, or configuration values."""


def _build_prompt(query: str, context_chunks: list[dict]) -> str:
    """Format retrieved chunks into a context block for the LLM."""
    context_parts = []
    for i, chunk in enumerate(context_chunks, 1):
        source = chunk.get("source", "unknown")
        doc_type = chunk.get("doc_type", "docs")
        text = chunk.get("text", "")
        context_parts.append(f"[{i}] ({doc_type}) {source}\n{text}")

    context_block = "\n\n---\n\n".join(context_parts)

    return f"""Use the following infrastructure knowledge base excerpts to answer the question.

CONTEXT:
{context_block}

QUESTION:
{query}

Answer based only on the context above. Cite source numbers like [1], [2] where relevant."""


async def generate_answer(
    query: str,
    context_chunks: list[dict],
    provider: Optional[str] = None,
) -> dict:
    """
    Generate a grounded answer using retrieved context chunks.

    Args:
        query: The user's question
        context_chunks: List of retrieved chunks from Qdrant
        provider: 'claude' or 'openai' — defaults to LLM_PROVIDER env var

    Returns:
        dict with 'answer', 'provider', and 'model' keys
    """
    provider = provider or os.getenv("LLM_PROVIDER", "claude")

    if provider == "claude":
        return await _call_claude(query, context_chunks)
    elif provider == "openai":
        return await _call_openai(query, context_chunks)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER '{provider}'. Use 'claude' or 'openai'.")


async def _call_claude(query: str, context_chunks: list[dict]) -> dict:
    """Call Anthropic Claude claude-sonnet-4-20250514."""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set.")

    client = anthropic.AsyncAnthropic(api_key=api_key)
    prompt = _build_prompt(query, context_chunks)

    message = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    return {
        "answer": message.content[0].text,
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
    }


async def _call_openai(query: str, context_chunks: list[dict]) -> dict:
    """Call OpenAI GPT-4o."""
    import openai

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set.")

    client = openai.AsyncOpenAI(api_key=api_key)
    prompt = _build_prompt(query, context_chunks)

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        max_tokens=1024,
    )

    return {
        "answer": response.choices[0].message.content,
        "provider": "openai",
        "model": "gpt-4o",
    }