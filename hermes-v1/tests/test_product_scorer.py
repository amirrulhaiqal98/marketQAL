"""
Tests for :mod:`modules.product_scorer`.

Two layers:

1. **Pure parsing tests** (``_parse_scoring_response``) — exhaustive
   coverage of every JSON shape MiniMax might return. These are the
   highest-signal tests because they catch contract drift instantly.
2. **Async integration test** (``async_score_product``) — exercises the
   full pipeline (input validation → prompt formatting → MiniMax call
   → response parsing → ``ScoringResult``) with ``httpx`` mocked by
   ``respx``. One happy-path test is enough here; the wrapper itself
   has its own dedicated smoke test in ``minimax.py`` runtime path.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from minimax import MiniMaxError
from modules.product_scorer import (
    _parse_scoring_response,
    async_score_product,
)


# ---------------------------------------------------------------------------
# Fixtures: canonical responses for the parsing tests
# ---------------------------------------------------------------------------

def _valid_payload(
    *,
    score: int = 78,
    fb: list[str] | None = None,
    th: list[str] | None = None,
    reasoning: str | None = None,
) -> dict:
    """Build a known-good JSON body for the parsing tests."""
    return {
        "score": score,
        "fb_hooks": fb if fb is not None else [
            "Tap kalau nak tahu apa yang semua orang beli bulan ni.",
            "RM19 je — nampak macam scam, tapi bukan.",
            "Sebelum scroll, tengok ni dulu.",
        ],
        "threads_hooks": th if th is not None else [
            "Hot take: product RM19 ni convert lagi baik dari yang RM100.",
            "Unpopular opinion: scroll-stop > copywriting.",
            "Saya test produk viral Shopee ni — jawapannya surprising.",
        ],
        "reasoning": reasoning if reasoning is not None else (
            "Strong curiosity gap from the price-point (RM19 vs typical RM50+ "
            "for this category). Scroll-stop potential is high because the "
            "visual angle is unusual."
        ),
    }


# ---------------------------------------------------------------------------
# /score input validation (synchronous, no network)
# ---------------------------------------------------------------------------

def test_parse_empty_string_raises():
    with pytest.raises(MiniMaxError, match="Empty MiniMax response"):
        _parse_scoring_response("")


def test_parse_whitespace_only_raises():
    with pytest.raises(MiniMaxError, match="Empty MiniMax response"):
        _parse_scoring_response("   \n\t  ")


def test_parse_empty_fence_raises():
    with pytest.raises(MiniMaxError, match="empty JSON fence"):
        _parse_scoring_response("```json\n```")


# ---------------------------------------------------------------------------
# /score happy path
# ---------------------------------------------------------------------------

def test_parse_bare_json_object():
    body = json.dumps(_valid_payload())
    result = _parse_scoring_response(body)
    assert result.score == 78
    assert len(result.fb_hooks) == 3
    assert len(result.threads_hooks) == 3
    assert "curiosity" in result.reasoning.lower()


def test_parse_json_with_json_fence():
    body = "```json\n" + json.dumps(_valid_payload(score=55)) + "\n```"
    result = _parse_scoring_response(body)
    assert result.score == 55


def test_parse_json_with_bare_fence():
    body = "```\n" + json.dumps(_valid_payload(score=33)) + "\n```"
    result = _parse_scoring_response(body)
    assert result.score == 33


def test_parse_json_with_surrounding_prose():
    body = (
        "Sure, here is the JSON you asked for:\n"
        + json.dumps(_valid_payload())
        + "\nHope that helps!"
    )
    result = _parse_scoring_response(body)
    assert result.score == 78
    assert len(result.fb_hooks) == 3


def test_parse_trims_whitespace_around_fence():
    body = "\n\n ```json\n" + json.dumps(_valid_payload()) + "\n``` \n\n"
    result = _parse_scoring_response(body)
    assert result.score == 78


def test_parse_score_zero_is_valid():
    result = _parse_scoring_response(json.dumps(_valid_payload(score=0)))
    assert result.score == 0


def test_parse_score_hundred_is_valid():
    result = _parse_scoring_response(json.dumps(_valid_payload(score=100)))
    assert result.score == 100


# ---------------------------------------------------------------------------
# /score score-field validation
# ---------------------------------------------------------------------------

def test_parse_missing_score_raises():
    payload = _valid_payload()
    del payload["score"]
    with pytest.raises(MiniMaxError, match="missing required field: 'score'"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_score_none_raises():
    payload = _valid_payload()
    payload["score"] = None
    with pytest.raises(MiniMaxError, match="missing required field: 'score'"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_score_string_raises():
    payload = _valid_payload()
    payload["score"] = "78"
    with pytest.raises(MiniMaxError, match="'score' must be an integer"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_score_negative_raises():
    payload = _valid_payload()
    payload["score"] = -1
    with pytest.raises(MiniMaxError, match="must be in \\[0, 100\\]"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_score_over_hundred_raises():
    payload = _valid_payload()
    payload["score"] = 101
    with pytest.raises(MiniMaxError, match="must be in \\[0, 100\\]"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_score_bool_raises():
    payload = _valid_payload()
    payload["score"] = True  # bool is a subclass of int — must be rejected
    with pytest.raises(MiniMaxError, match="got bool"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_score_float_rounds_to_int():
    payload = _valid_payload()
    payload["score"] = 78.4
    result = _parse_scoring_response(json.dumps(payload))
    assert result.score == 78
    assert isinstance(result.score, int)


def test_parse_score_float_rounds_half_up():
    payload = _valid_payload()
    payload["score"] = 78.6
    result = _parse_scoring_response(json.dumps(payload))
    assert result.score == 79


def test_parse_score_nan_raises():
    payload = _valid_payload()
    # JSON spec disallows NaN, but be defensive: build then inject.
    body = json.dumps(payload).replace('"score": 78', '"score": NaN', 1)
    with pytest.raises((MiniMaxError, ValueError)):
        _parse_scoring_response(body)


# ---------------------------------------------------------------------------
# /score hook-list validation
# ---------------------------------------------------------------------------

def test_parse_missing_fb_hooks_raises():
    payload = _valid_payload()
    del payload["fb_hooks"]
    with pytest.raises(MiniMaxError, match="missing required field: 'fb_hooks'"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_missing_threads_hooks_raises():
    payload = _valid_payload()
    del payload["threads_hooks"]
    with pytest.raises(MiniMaxError, match="missing required field: 'threads_hooks'"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_fb_hooks_too_few_raises():
    payload = _valid_payload(fb=["only one"])
    with pytest.raises(MiniMaxError, match="'fb_hooks' must contain at least 3"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_threads_hooks_empty_list_raises():
    payload = _valid_payload(th=[])
    with pytest.raises(MiniMaxError, match="'threads_hooks' must contain at least 3"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_fb_hooks_not_a_list_raises():
    payload = _valid_payload()
    payload["fb_hooks"] = "not a list"
    with pytest.raises(MiniMaxError, match="'fb_hooks' must be a list"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_fb_hooks_non_string_element_raises():
    payload = _valid_payload()
    payload["fb_hooks"] = ["ok", "ok", 42]
    with pytest.raises(MiniMaxError, match="'fb_hooks\\[2\\]' must be a string"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_fb_hooks_empty_string_element_raises():
    payload = _valid_payload()
    payload["fb_hooks"] = ["ok", "   ", "ok"]
    with pytest.raises(MiniMaxError, match="'fb_hooks\\[1\\]' is empty"):
        _parse_scoring_response(json.dumps(payload))



def test_parse_hook_too_long_raises():
    long_hook = "x" * 91  # one over the 90-char limit
    payload = _valid_payload(fb=["ok", "ok", long_hook])
    with pytest.raises(MiniMaxError, match="91 chars, max is 90"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_hook_at_max_length_is_ok():
    edge_hook = "y" * 90  # exactly at limit
    payload = _valid_payload(fb=["ok", "ok", edge_hook])
    result = _parse_scoring_response(json.dumps(payload))
    assert result.fb_hooks[2] == edge_hook


def test_parse_extra_hooks_are_truncated():
    """If MiniMax returns 5 hooks, we keep the first 3 (not raise)."""
    payload = _valid_payload(fb=["a", "b", "c", "d", "e"])
    result = _parse_scoring_response(json.dumps(payload))
    assert result.fb_hooks == ["a", "b", "c"]


def test_parse_whitespace_in_hooks_trimmed():
    # Leading/trailing whitespace is trimmed; internal whitespace stays
    # so we don't accidentally munge multi-line copy.
    payload = _valid_payload(fb=["  hook one  ", "hook two\t", "hook\nthree"])
    result = _parse_scoring_response(json.dumps(payload))
    assert result.fb_hooks == ["hook one", "hook two", "hook\nthree"]



# ---------------------------------------------------------------------------
# /score reasoning validation
# ---------------------------------------------------------------------------

def test_parse_missing_reasoning_raises():
    payload = _valid_payload()
    del payload["reasoning"]
    with pytest.raises(MiniMaxError, match="missing required field: 'reasoning'"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_empty_reasoning_raises():
    payload = _valid_payload(reasoning="")
    with pytest.raises(MiniMaxError, match="too short"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_short_reasoning_raises():
    payload = _valid_payload(reasoning="ok")
    with pytest.raises(MiniMaxError, match="too short"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_reasoning_not_string_raises():
    payload = _valid_payload()
    payload["reasoning"] = 12345
    with pytest.raises(MiniMaxError, match="'reasoning' must be a string"):
        _parse_scoring_response(json.dumps(payload))


def test_parse_reasoning_truncated_when_too_long():
    """Long reasoning is truncated to 600 chars (not rejected)."""
    long_reasoning = "x" * 1000
    payload = _valid_payload(reasoning=long_reasoning)
    result = _parse_scoring_response(json.dumps(payload))
    assert len(result.reasoning) == 600
    assert result.reasoning == "x" * 600


def test_parse_reasoning_whitespace_trimmed():
    payload = _valid_payload(reasoning="   plenty long enough here, thanks   ")
    result = _parse_scoring_response(json.dumps(payload))
    assert result.reasoning == "plenty long enough here, thanks"


# ---------------------------------------------------------------------------
# /score structural / JSON failures
# ---------------------------------------------------------------------------

def test_parse_invalid_json_raises():
    with pytest.raises(MiniMaxError, match="not valid JSON"):
        _parse_scoring_response("{not json")


def test_parse_json_array_root_raises():
    with pytest.raises(MiniMaxError, match="must be an object"):
        _parse_scoring_response("[1, 2, 3]")


def test_parse_json_string_root_raises():
    with pytest.raises(MiniMaxError, match="must be an object"):
        _parse_scoring_response('"just a string"')


def test_parse_json_null_root_raises():
    with pytest.raises(MiniMaxError, match="must be an object"):
        _parse_scoring_response("null")


def test_parse_extra_fields_are_ignored_not_rejected():
    """Extra fields in the response must not break parsing."""
    payload = _valid_payload()
    payload["_extra_meta"] = {"model": "fiq/minimax-m3", "tokens": 123}
    payload["debug"] = [1, 2, 3]
    result = _parse_scoring_response(json.dumps(payload))
    assert result.score == 78


# ---------------------------------------------------------------------------
# async_score_product — input validation (synchronous, before any network)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_score_product_empty_title_raises_value_error():
    with pytest.raises(ValueError, match="non-empty title"):
        await async_score_product("", 10.0, "Electronics")


@pytest.mark.asyncio
async def test_async_score_product_whitespace_title_raises_value_error():
    with pytest.raises(ValueError, match="non-empty title"):
        await async_score_product("   \t\n", 10.0, "Electronics")


@pytest.mark.asyncio
async def test_async_score_product_empty_category_raises_value_error():
    with pytest.raises(ValueError, match="non-empty category"):
        await async_score_product("Cool product", 10.0, "")


@pytest.mark.asyncio
async def test_async_score_product_negative_price_raises_value_error():
    with pytest.raises(ValueError, match="price must be >= 0"):
        await async_score_product("Cool product", -1.0, "Electronics")


# ---------------------------------------------------------------------------
# async_score_product — happy path via respx mock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_async_score_product_full_pipeline_success():
    """Mock the MiniMax /chat/completions endpoint and verify the
    full pipeline returns a populated ``ScoringResult``."""
    body = json.dumps(_valid_payload(score=82))
    respx.post("https://minimax.afiqstoreapi.cloud/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": body}},
                ],
            },
        ),
    )

    result = await async_score_product(
        title="Glove viral Shopee",
        price=19.90,
        category="Fashion",
    )

    assert isinstance(result.score, int)
    assert result.score == 82
    assert len(result.fb_hooks) == 3
    assert len(result.threads_hooks) == 3
    assert "curiosity" in result.reasoning.lower()

    # Verify exactly one outbound call was made.
    assert respx.calls.call_count == 1
    request = respx.calls.last.request
    assert request.method == "POST"
    assert request.url.path == "/v1/chat/completions"


    # The prompt body should contain the formatted product input.
    sent = json.loads(request.content)
    user_messages = [m for m in sent["messages"] if m["role"] == "user"]
    assert any("Glove viral Shopee" in m["content"] for m in user_messages)
    assert any("19.90" in m["content"] for m in user_messages)
    assert any("Fashion" in m["content"] for m in user_messages)
    # And the system persona should be attached.
    assert any(m["role"] == "system" for m in sent["messages"])


@pytest.mark.asyncio
@respx.mock
async def test_async_score_product_handles_markdown_fenced_response():
    """Some MiniMax responses wrap the JSON in ```json ... ``` — must strip."""
    body = "```json\n" + json.dumps(_valid_payload(score=44)) + "\n```"
    respx.post("https://minimax.afiqstoreapi.cloud/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": body}},
                ],
            },
        ),
    )

    result = await async_score_product(
        title="A product",
        price=10.0,
        category="Misc",
    )
    assert result.score == 44


@pytest.mark.asyncio
@respx.mock
async def test_async_score_product_propagates_minimax_error():
    """If MiniMax returns a contract-violating body, MiniMaxError surfaces."""
    bad_body = json.dumps({"score": 999, "fb_hooks": [], "threads_hooks": [], "reasoning": ""})
    respx.post("https://minimax.afiqstoreapi.cloud/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": bad_body}},
                ],
            },
        ),
    )

    with pytest.raises(MiniMaxError):
        await async_score_product("X", 10.0, "Y")


@pytest.mark.asyncio
@respx.mock
async def test_async_score_product_propagates_http_error():
    """If MiniMax returns 500, MiniMaxError is raised with status detail."""
    respx.post("https://minimax.afiqstoreapi.cloud/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"error": "boom"}),
    )

    with pytest.raises(MiniMaxError, match="HTTP 500"):
        await async_score_product("X", 10.0, "Y")
