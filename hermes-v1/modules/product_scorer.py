"""
Product clickability scorer.

Wraps the MiniMax chat-completions call behind a typed ``ScoringResult``
so callers (``bot.py``, future batch scripts) don't have to deal with
JSON, markdown fences, or missing fields.

Pipeline:
    1. ``async_score_product(title, price, category)`` validates inputs,
       formats ``PRODUCT_SCORING_PROMPT`` and calls ``async_minimax_chat``
       using the shared ``HERMES_SYSTEM_PERSONA``.
    2. The raw assistant text is passed to ``_parse_scoring_response``,
       which strips markdown code fences, parses JSON, validates shape
       and types, and returns a ``ScoringResult``.

Design rules:

* No Telegram imports — this module is callable from any async context.
* Input validation is synchronous (``ValueError``) so mis-use fails
  fast without a network round-trip.
* All LLM-response validation lives in ``_parse_scoring_response`` and
  raises ``MiniMaxError`` (never ``ValueError``) so callers can tell
  user errors from API-contract errors.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from minimax import MiniMaxError, async_minimax_chat
from prompts import HERMES_SYSTEM_PERSONA, PRODUCT_SCORING_PROMPT

from modules.constants import DEFAULT_MINIMAX_TEMPERATURE, DEFAULT_MINIMAX_MAX_TOKENS
from modules.models import ScoringResult


logger = logging.getLogger(__name__)


__all__ = ["async_score_product", "_parse_scoring_response"]


# Output-contract limits — kept here (not in constants.py) because they
# are part of the product_scorer / MiniMax JSON contract, not global
# Hermes thresholds.
_MIN_HOOKS_PER_PLATFORM = 3
_MAX_HOOK_LENGTH = 90
_MIN_REASONING_LEN = 10
_MAX_REASONING_LEN = 600
_SCORE_MIN = 0
_SCORE_MAX = 100


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def async_score_product(
    title: str,
    price: float,
    category: str,
) -> ScoringResult:
    """Score a product's clickability via MiniMax and return a typed result.

    Args:
        title: Product title as shown on Shopee. Must be non-empty.
        price: Listed price in MYR. Must be ``>= 0``.
        category: Shopee category string (e.g. ``"Electronics"``).
            Must be non-empty.

    Returns:
        ``ScoringResult`` with score (0-100), 3 FB hooks, 3 Threads
        hooks, and a 2-3 sentence reasoning string.

    Raises:
        ValueError: On empty/invalid inputs (caller error).
        MiniMaxError: On transport failure, non-2xx response, malformed
            JSON, or contract violations from the model output.
    """
    title = (title or "").strip()
    category = (category or "").strip()
    if not title:
        raise ValueError("async_score_product requires a non-empty title")
    if not category:
        raise ValueError("async_score_product requires a non-empty category")
    if price < 0:
        raise ValueError(f"price must be >= 0, got {price!r}")

    prompt = PRODUCT_SCORING_PROMPT.format(
        title=title,
        price=f"{price:.2f}",
        category=category,
    )

    raw = await async_minimax_chat(
        prompt,
        system=HERMES_SYSTEM_PERSONA,
        temperature=DEFAULT_MINIMAX_TEMPERATURE,
        max_tokens=DEFAULT_MINIMAX_MAX_TOKENS,
    )
    logger.debug("product scorer raw response: %s", raw[:200])

    return _parse_scoring_response(raw)


# ---------------------------------------------------------------------------
# Parsing (pure, fully unit-tested)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)


def _parse_scoring_response(raw: str) -> ScoringResult:
    """Parse a MiniMax JSON response into a :class:`ScoringResult`.

    Handles:

    * Markdown code fences (triple-backtick json ... triple-backtick) — stripped.
    * Surrounding whitespace and prose around the JSON object.
    * Score coercion from float (some models emit ``85.0``).
    * Hook-list truncation (keep first 3) and string coercion.
    * Length checks on individual hooks and the reasoning paragraph.


    Raises:
        MiniMaxError: On empty body, unparseable JSON, missing fields,
            wrong types, out-of-range score, too few / too many hooks,
            oversize hooks, or empty / oversize reasoning.
    """
    if not raw or not raw.strip():
        raise MiniMaxError("Empty MiniMax response — cannot parse scoring result.")

    json_text = _strip_fence(raw)
    if not json_text:
        # Fence matched but was empty.
        raise MiniMaxError("MiniMax response contained an empty JSON fence.")

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise MiniMaxError(
            f"MiniMax response is not valid JSON: {exc.msg} "
            f"(at pos {exc.pos}). Body excerpt: {_excerpt(raw)}"
        ) from exc

    if not isinstance(data, dict):
        raise MiniMaxError(
            f"MiniMax JSON root must be an object, got {type(data).__name__}."
        )

    score = _parse_score(data.get("score"))
    fb_hooks = _parse_hook_list(
        data.get("fb_hooks"), field_name="fb_hooks",
    )
    threads_hooks = _parse_hook_list(
        data.get("threads_hooks"), field_name="threads_hooks",
    )
    reasoning = _parse_reasoning(data.get("reasoning"))

    return ScoringResult(
        score=score,
        fb_hooks=fb_hooks,
        threads_hooks=threads_hooks,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Field-level helpers
# ---------------------------------------------------------------------------

def _parse_score(value: Any) -> int:
    """Validate and coerce the ``score`` field to ``int`` in [0, 100]."""
    if value is None:
        raise MiniMaxError("MiniMax JSON missing required field: 'score'.")
    if isinstance(value, bool):
        # bool is a subclass of int — guard explicitly.
        raise MiniMaxError(f"'score' must be an integer 0-100, got bool {value!r}.")
    if isinstance(value, float):
        if value != value:  # NaN
            raise MiniMaxError(f"'score' is NaN; expected integer 0-100.")
        # Round half-away-from-zero for stability.
        value = int(round(value))
    if not isinstance(value, int):
        raise MiniMaxError(
            f"'score' must be an integer 0-100, got {type(value).__name__}: {value!r}."
        )
    if not _SCORE_MIN <= value <= _SCORE_MAX:
        raise MiniMaxError(
            f"'score' must be in [{_SCORE_MIN}, {_SCORE_MAX}], got {value}."
        )
    return value


def _parse_hook_list(value: Any, *, field_name: str) -> list[str]:
    """Validate a hook list: exactly ``_MIN_HOOKS_PER_PLATFORM`` strings,
    each ``<= _MAX_HOOK_LENGTH`` characters after coercion.
    """
    if value is None:
        raise MiniMaxError(f"MiniMax JSON missing required field: '{field_name}'.")
    if not isinstance(value, list):
        raise MiniMaxError(
            f"'{field_name}' must be a list of strings, got {type(value).__name__}."
        )
    if len(value) < _MIN_HOOKS_PER_PLATFORM:
        raise MiniMaxError(
            f"'{field_name}' must contain at least {_MIN_HOOKS_PER_PLATFORM} "
            f"strings, got {len(value)}."
        )

    hooks: list[str] = []
    for idx, item in enumerate(value[:_MIN_HOOKS_PER_PLATFORM]):
        if not isinstance(item, str):
            raise MiniMaxError(
                f"'{field_name}[{idx}]' must be a string, got {type(item).__name__}."
            )
        text = item.strip()
        if not text:
            raise MiniMaxError(f"'{field_name}[{idx}]' is empty after trimming.")
        if len(text) > _MAX_HOOK_LENGTH:
            raise MiniMaxError(
                f"'{field_name}[{idx}]' is {len(text)} chars, max is "
                f"{_MAX_HOOK_LENGTH}: {text!r}"
            )
        hooks.append(text)

    return hooks


def _parse_reasoning(value: Any) -> str:
    """Validate the ``reasoning`` field: non-empty string, bounded length."""
    if value is None:
        raise MiniMaxError("MiniMax JSON missing required field: 'reasoning'.")
    if not isinstance(value, str):
        raise MiniMaxError(
            f"'reasoning' must be a string, got {type(value).__name__}."
        )
    text = value.strip()
    if len(text) < _MIN_REASONING_LEN:
        raise MiniMaxError(
            f"'reasoning' too short ({len(text)} chars), "
            f"need at least {_MIN_REASONING_LEN}."
        )
    if len(text) > _MAX_REASONING_LEN:
        # Truncate rather than reject — model occasionally rambles.
        text = text[:_MAX_REASONING_LEN].rstrip()
    return text


def _strip_fence(raw: str) -> str:
    """Extract the first JSON object from a MiniMax response body.

    Handles three common response shapes, in priority order:

    1. ``\\`\\`\\`json\\n{...}\\n\\`\\`\\`  `` — markdown JSON fence.
    2. ``\\`\\`\\`\\n{...}\\n\\`\\`\\`      `` — bare markdown fence.
    3. ``"Here is the JSON: {...} hope that helps!"`` — JSON object
       embedded inside surrounding prose (very common with chat-tuned
       models that "explain" before/after the structured output).

    Returns the JSON object text on success. Returns ``""`` if a fence
    was found but its contents are empty. Returns the raw text
    unchanged if no fence and no ``{...}`` substring is found — in
    that case ``json.loads`` will raise naturally and the caller will
    surface a clean :class:`MiniMaxError`.
    """
    text = raw.strip()

    # Path 1 & 2: explicit code fence.
    match = _FENCE_RE.match(text)
    if match:
        inner = match.group(1).strip()
        return inner  # may be ""; caller handles empty-fence error.

    # Path 3: extract the first balanced JSON object from prose.
    start = text.find("{")
    if start == -1:
        return text  # no JSON-looking content; let json.loads fail.
    return _extract_balanced_object(text, start)


def _extract_balanced_object(text: str, start: int) -> str:
    """Return the substring ``text[start:end+1]`` for the first balanced
    ``{...}`` JSON object starting at ``start``.

    Walks the string character-by-character, respecting JSON string
    boundaries and backslash escapes, so braces inside string literals
    don't fool the depth counter.
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    # Unbalanced — return as-is and let json.loads produce the error.
    return text[start:]



def _excerpt(raw: str, limit: int = 160) -> str:
    """Return a short single-line excerpt for error messages."""
    text = (raw or "").strip().replace("\n", " ").replace("\r", " ")
    if len(text) > limit:
        return text[:limit] + "…"
    return text
