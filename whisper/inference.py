"""
LLM inference for Whisper Network task execution.

Primary backend: Ollama (http://localhost:11434).
Fallback: keyword search over shard lines (original mock behaviour).

Environment variables:
  OLLAMA_BASE_URL   — override Ollama endpoint (default: http://localhost:11434)
  WHISPER_MODEL     — model name to use            (default: llama3.2)
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

_DEFAULT_MODEL    = "llama3.2"
_DEFAULT_BASE_URL = "http://localhost:11434"
_TIMEOUT          = 180  # seconds — must cover full Ollama queue (6 nodes × ~30s/req)

_CAPABILITY_PROMPTS = {
    "search": (
        "You are a precise search assistant. "
        "Find and quote the most relevant information from the provided context "
        "that answers the query. Be concise and cite the source text."
    ),
    "summarize": (
        "You are a summarization assistant. "
        "Using only the provided context, produce a clear and concise summary "
        "that directly answers the query. Avoid padding."
    ),
    "reason": (
        "You are a reasoning assistant. "
        "Think step by step using the provided context to answer the query. "
        "Show your reasoning briefly before giving the final answer."
    ),
}
_DEFAULT_PROMPT = (
    "You are a helpful assistant. "
    "Answer the query using the provided context. Be concise."
)


def run(payload: str, context_lines: list, capabilities: set, shard_id: int, system_prompt: str = None) -> str:
    """Run inference. Tries Ollama; falls back to keyword mock on any error."""
    base_url = os.environ.get("OLLAMA_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
    model    = os.environ.get("WHISPER_MODEL", _DEFAULT_MODEL)

    query = payload.strip()
    if query.lower().startswith("query:"):
        query = query[6:].strip()

    # Use provided system prompt or fall back to capability-based prompt
    cap = None
    if not system_prompt:
        cap = next((c for c in ("reason", "summarize", "search") if c in capabilities), None)
        system_prompt = _CAPABILITY_PROMPTS.get(cap, _DEFAULT_PROMPT)

    context      = "\n".join(context_lines) if context_lines else "(no context available)"
    user_message = f"Context:\n{context}\n\nQuery: {query}"

    try:
        resp = requests.post(
            f"{base_url}/api/chat",
            json={
                "model":   model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_message},
                ],
                "stream": False,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        answer = resp.json()["message"]["content"].strip()
        logger.info("ollama ok: shard-%d model=%s cap=%s", shard_id, model, cap or "agent")
        return f"[{model}] {answer}"
    except requests.exceptions.ConnectionError:
        logger.warning("ollama unreachable at %s — falling back to keyword search", base_url)
    except requests.exceptions.Timeout:
        logger.warning("ollama timeout (shard-%d, %ds) — falling back to keyword search", shard_id, _TIMEOUT)
    except requests.exceptions.HTTPError as exc:
        logger.warning(
            "ollama http error (shard-%d): %s %s — falling back",
            shard_id, exc.response.status_code, exc.response.text[:200]
        )
    except Exception as exc:
        logger.warning("ollama inference failed (shard-%d): %s — falling back", shard_id, exc)

    return _keyword_fallback(query, context_lines, shard_id)


def _keyword_fallback(query: str, lines: list, shard_id: int) -> str:
    query_lower = query.lower()
    matches = [line for line in lines if query_lower in line.lower()]
    if matches:
        preview = " | ".join(matches[:3])
        if len(preview) > 120:
            preview = preview[:117] + "..."
        return f"shard-{shard_id}: {len(matches)} match(es): {preview}"
    return f"shard-{shard_id}: no matches for '{query}'"
