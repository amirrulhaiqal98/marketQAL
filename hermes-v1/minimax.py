"""
MiniMax API wrapper.

Hermes uses the MiniMax chat-completions endpoint (OpenAI-compatible) to
power the product scorer, Meta analyzer narrative, and insight summaries.
This module is the **only** place in the codebase that talks HTTP to
MiniMax — every other module (``product_scorer``, ``insight_engine`` ...)
imports from here so we have a single seam for retries, logging, and
error mapping.

Design rules (from ``docs/DEVELOPMENT_RULES.md``):

* No Telegram imports. Pure async HTTP.
* No silent failures — every error raises ``MiniMaxError`` with enough
  context for ``bot.py`` to render a useful Telegram message.
* Bearer-token auth from env (``MINIMAX_API_KEY``).

Usage::

    from minimax import async_minimax_chat

    text = await async_minimax_chat(
        "Score this product ...",
        system="You are Hermes, an affiliate traffic analyst.",
        temperature=0.4,
        max_tokens=512,
    )

The wrapper also exposes a singleton ``get_client()`` for callers that
want to issue many requests in parallel (e.g. scoring several products
at once) without paying the TLS handshake cost per call.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv

# ``.env`` is loaded once at import time. ``bot.py`` also calls
# ``load_dotenv()`` at startup; calling it twice is idempotent and cheap.
load_dotenv()


logger = logging.getLogger(__name__)


__all__ = [
    "async_minimax_chat",
    "get_client",
    "MiniMaxError",
    "close_client",
]


# ---------------------------------------------------------------------------
# Public exception (defined first so the env-loader below can raise it)
# ---------------------------------------------------------------------------

class MiniMaxError(RuntimeError):
    """Raised on any non-recoverable MiniMax API failure.

    The wrapped ``detail`` string is safe to surface to the user via
    Telegram — it intentionally omits the API key and request body.
    """


# ---------------------------------------------------------------------------
# Configuration — read from env at import time so missing keys fail loud
# ---------------------------------------------------------------------------

def _required_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise MiniMaxError(
            f"Missing required environment variable: {key}. "
            f"Set it in hermes-v1/.env before running Hermes."
        )
    return value


_API_KEY: str = _required_env("MINIMAX_API_KEY")
_BASE_URL: str = os.getenv("MINIMAX_BASE_URL", "https://minimax.afiqstoreapi.cloud/v1").rstrip("/")
_DEFAULT_MODEL: str = os.getenv("MINIMAX_MODEL", "fiq/minimax-m3")

# Endpoint path is relative to the versioned base URL.
_CHAT_COMPLETIONS_PATH = "/chat/completions"

# Network / retry knobs. Kept conservative so a stalled API doesn't hang
# the whole Telegram bot loop.
_REQUEST_TIMEOUT_SECONDS = 30.0
_CONNECT_TIMEOUT_SECONDS = 10.0
_MAX_RETRIES = 1               # 1 retry = 2 total attempts on transient failures
_RETRY_BACKOFF_SECONDS = 1.0   # exponential: 1s, 2s
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# Singleton httpx client
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


def get_client() -> httpx.AsyncClient:
    """Return a process-wide :class:`httpx.AsyncClient`.

    A single client is reused across calls so the underlying TLS session
    pool, HTTP/2 connection, and DNS resolver are amortised. ``bot.py``
    should call :func:`close_client` during graceful shutdown.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=httpx.Timeout(
                _REQUEST_TIMEOUT_SECONDS,
                connect=_CONNECT_TIMEOUT_SECONDS,
            ),
            headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "hermes-v1/0.1 (+https://github.com/local/hermes)",
            },
        )
    return _client


async def close_client() -> None:
    """Close the singleton client. Safe to call when never created."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def async_minimax_chat(
    prompt: str,
    *,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: int = 1024,
    model: str | None = None,
) -> str:
    """Send a single-turn chat prompt to MiniMax and return assistant text.

    Args:
        prompt: The user-role message. Must be non-empty.
        system: Optional system-role message (persona / instructions).
        temperature: Sampling temperature in ``[0.0, 2.0]``. Default ``0.7``.
        max_tokens: Hard cap on the response length. Default ``1024``.
        model: Override the model from env (defaults to ``MINIMAX_MODEL``).

    Returns:
        The assistant message text (str). Never empty — raises on the
        unusual case where MiniMax returns a 200 with no content.

    Raises:
        ValueError: If ``prompt`` is empty or ``temperature``/``max_tokens``
            are outside their valid ranges.
        MiniMaxError: For any transport, HTTP, or schema-level failure.
    """
    if not prompt or not prompt.strip():
        raise ValueError("async_minimax_chat requires a non-empty prompt")
    if not 0.0 <= temperature <= 2.0:
        raise ValueError(f"temperature must be in [0.0, 2.0], got {temperature!r}")
    if max_tokens <= 0:
        raise ValueError(f"max_tokens must be > 0, got {max_tokens!r}")

    payload = _build_payload(
        prompt=prompt,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model or _DEFAULT_MODEL,
    )

    client = get_client()
    url = _CHAT_COMPLETIONS_PATH

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            logger.debug(
                "MiniMax request attempt=%d model=%s prompt_len=%d",
                attempt + 1, payload["model"], len(prompt),
            )
            response = await client.post(url, json=payload)
            return await _handle_response(response)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
            logger.warning(
                "MiniMax transport error attempt=%d/%d: %s",
                attempt + 1, _MAX_RETRIES + 1, exc,
            )
        except MiniMaxError as exc:
            # Only retry on transient HTTP statuses; surface everything else.
            if getattr(exc, "_retryable", False) and attempt < _MAX_RETRIES:
                last_error = exc
                logger.warning(
                    "MiniMax retryable error attempt=%d/%d: %s",
                    attempt + 1, _MAX_RETRIES + 1, exc,
                )
            else:
                raise

        if attempt < _MAX_RETRIES:
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (2 ** attempt))

    raise MiniMaxError(
        f"MiniMax request failed after {_MAX_RETRIES + 1} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable, no I/O)
# ---------------------------------------------------------------------------

def _build_payload(
    prompt: str,
    system: str,
    temperature: float,
    max_tokens: int,
    model: str,
) -> dict[str, Any]:
    """Build the OpenAI-compatible request body.

    Kept as a free function so it can be unit-tested without HTTP.
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    return {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


async def _handle_response(response: httpx.Response) -> str:
    """Validate the response and extract ``choices[0].message.content``.

    Raises:
        MiniMaxError: On non-2xx, malformed body, or empty content.
    """
    if response.status_code < 200 or response.status_code >= 300:
        body_excerpt = _safe_body_excerpt(response)
        retryable = response.status_code in _RETRYABLE_STATUS_CODES
        err = MiniMaxError(
            f"MiniMax HTTP {response.status_code}: {body_excerpt}"
        )
        # Tag so the retry loop in async_minimax_chat can decide.
        setattr(err, "_retryable", retryable)
        logger.error("MiniMax non-2xx status=%d body=%s", response.status_code, body_excerpt)
        raise err

    try:
        data = response.json()
    except ValueError as exc:
        raise MiniMaxError(
            f"MiniMax returned non-JSON body: {_safe_body_excerpt(response)}"
        ) from exc

    try:
        choices = data["choices"]
        first = choices[0]
        message = first["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MiniMaxError(
            f"MiniMax response missing choices[0].message.content: "
            f"{_safe_body_excerpt(response)}"
        ) from exc

    if not isinstance(content, str) or not content.strip():
        raise MiniMaxError(
            f"MiniMax returned empty content: {_safe_body_excerpt(response)}"
        )

    return content


def _safe_body_excerpt(response: httpx.Response, limit: int = 240) -> str:
    """Return a short, single-line excerpt of the response body for logs."""
    try:
        text = response.text or ""
    except Exception:  # noqa: BLE001 — defensive against binary/encoding errors
        return "<unreadable body>"
    text = text.strip().replace("\n", " ").replace("\r", " ")
    if len(text) > limit:
        return text[:limit] + "…"
    return text or "<empty body>"
