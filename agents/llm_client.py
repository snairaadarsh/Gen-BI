"""
agents/llm_client.py
---------------------
Unified LLM client that abstracts over three providers:

  - "gemini"     → Google Gemini via langchain-google-genai (default)
  - "openrouter" → Any model on OpenRouter via langchain-openai's
                   OpenAI-compatible client (requires OPENROUTER_API_KEY in .env)
  - "ollama"     → Local Ollama instance (requires Ollama running on localhost)

Exposes LangChain `BaseChatModel` instances so SqlAgent / BiAgent can be
built as LangChain / LangGraph chains.

Usage (in an agent):
    from agents.llm_client import get_chat_model, get_completion

    llm = get_chat_model(provider="openrouter", model="anthropic/claude-3-5-sonnet")
    response = llm.invoke([...])

    text = get_completion(
        system_prompt="You are ...",
        user_prompt="...",
        provider="openrouter",
        model="anthropic/claude-3-5-sonnet",
    )

FIX #13: Replaced @lru_cache on stateful LangChain model objects with an
explicit thread-safe dict cache + threading.Lock to avoid race conditions
under concurrent requests.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# ── Default model names ───────────────────────────────────────────────────────

GEMINI_DEFAULT_MODEL     = "gemini-2.0-flash"
OPENROUTER_DEFAULT_MODEL = "openai/gpt-4o-mini"
OPENROUTER_BASE_URL      = "https://openrouter.ai/api/v1"
OLLAMA_DEFAULT_MODEL     = "llama3.1:8b"
OLLAMA_BASE_URL          = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# Thread-safe model cache: (provider, model, temperature) → BaseChatModel
_model_cache: dict[tuple[str, str, float], Any] = {}
_model_cache_lock = threading.Lock()


# ── LangChain chat model factory ──────────────────────────────────────────────

def get_chat_model(provider: str = "gemini", model: str | None = None, temperature: float = 0.0):
    """
    Returns a LangChain BaseChatModel for the given provider/model.

    Models are cached per (provider, model, temperature) with a threading lock
    so repeated calls within a session reuse the same client safely under
    concurrent requests.

    Args:
        provider:    "gemini", "openrouter", or "ollama".
        model:       Model ID override. Falls back to provider default.
        temperature: Sampling temperature (0.0 = deterministic, good for SQL gen).

    Returns:
        A LangChain chat model instance implementing .invoke() / .stream().
    """
    resolved_model = model or _default_model(provider)
    cache_key = (provider, resolved_model, temperature)

    with _model_cache_lock:
        if cache_key not in _model_cache:
            _model_cache[cache_key] = _build_chat_model(provider, resolved_model, temperature)
        return _model_cache[cache_key]


def _default_model(provider: str) -> str:
    if provider == "gemini":
        return GEMINI_DEFAULT_MODEL
    if provider == "openrouter":
        return OPENROUTER_DEFAULT_MODEL
    if provider == "ollama":
        return OLLAMA_DEFAULT_MODEL
    raise ValueError(f"Unknown LLM provider '{provider}'. Expected 'gemini', 'openrouter', or 'ollama'.")


def _build_chat_model(provider: str, model: str, temperature: float):
    """Construct a fresh LangChain chat model instance."""
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set in the environment.")
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            temperature=temperature,
        )

    if provider == "openrouter":
        from langchain_openai import ChatOpenAI
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is not set in the environment. "
                "Get a free key at https://openrouter.ai/keys and add it to your .env file."
            )
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            temperature=temperature,
            default_headers={
                "HTTP-Referer": "https://github.com/genbi-poc",
                "X-Title": "GenBI",
            },
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=model,
            base_url=OLLAMA_BASE_URL,
            temperature=temperature,
        )

    raise ValueError(f"Unknown LLM provider '{provider}'. Expected 'gemini', 'openrouter', or 'ollama'.")


def get_embeddings_model(provider: str = "local"):
    """
    Returns a LangChain Embeddings instance used by the FAISS-backed
    MetadataAgent and RagAgent retrievers.

    "local" (default) uses HuggingFaceEmbeddings with all-MiniLM-L6-v2
    so existing FAISS indexes remain compatible.
    """
    from langchain_huggingface import HuggingFaceEmbeddings
    return HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")


# ── Legacy plain-text completion API (kept for backward compatibility) ────────

def get_completion(
    system_prompt: str,
    user_prompt: str,
    provider: str = "gemini",
    model: str | None = None,
) -> str:
    """
    Call the appropriate LLM and return the response text.

    Retained for any code that still wants a simple text-in/text-out call.
    Internally routes through get_chat_model so there is a single source of
    truth for provider/model configuration.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = get_chat_model(provider=provider, model=model)
    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ])
    return (response.content or "").strip()