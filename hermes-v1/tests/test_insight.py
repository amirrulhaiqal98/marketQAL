"""
Unit tests for modules.insight_engine.classify_epc.

Pure tests — no fixtures, no I/O, no async. The classification threshold
is the most safety-critical constant in Hermes (it drives buy/kill decisions
on real ad spend), so it gets explicit boundary coverage.
"""

from __future__ import annotations

import pytest

from modules.constants import (
    CLASSIFICATIONS,
    EPC_KILL_THRESHOLD,
    EPC_SCALE_THRESHOLD,
)
from modules.insight_engine import classify_epc, classify_many


# ---------------------------------------------------------------------------
# Spec-mandated edge cases (from implementation_plan.md §[Testing])
# ---------------------------------------------------------------------------

def test_classify_above_scale_threshold_is_scale():
    """0.06 RM/click -> SCALE."""
    assert classify_epc(0.06) == "SCALE"


def test_classify_mid_range_is_hold():
    """0.03 RM/click -> HOLD (between KILL < 0.01 and SCALE >= 0.05)."""
    assert classify_epc(0.03) == "HOLD"


def test_classify_below_kill_threshold_is_kill():
    """0.005 RM/click -> KILL."""
    assert classify_epc(0.005) == "KILL"


# ---------------------------------------------------------------------------
# Threshold-boundary tests — off-by-one is the most likely bug here
# ---------------------------------------------------------------------------

def test_classify_exactly_at_scale_threshold_is_scale():
    """EPC == 0.05 must be SCALE (rule is >=, not >)."""
    assert classify_epc(EPC_SCALE_THRESHOLD) == "SCALE"


def test_classify_just_below_scale_threshold_is_hold():
    """EPC == 0.049999 -> HOLD (under SCALE, above KILL)."""
    assert classify_epc(EPC_SCALE_THRESHOLD - 0.000001) == "HOLD"


def test_classify_exactly_at_kill_threshold_is_hold():
    """EPC == 0.01 must be HOLD (rule is < 0.01 -> KILL, so == 0.01 stays HOLD)."""
    assert classify_epc(EPC_KILL_THRESHOLD) == "HOLD"


def test_classify_just_below_kill_threshold_is_kill():
    """EPC == 0.009999 -> KILL."""
    assert classify_epc(EPC_KILL_THRESHOLD - 0.000001) == "KILL"


# ---------------------------------------------------------------------------
# Zero and negative EPC
# ---------------------------------------------------------------------------

def test_classify_zero_epc_is_kill():
    """A zero-click / zero-commission campaign has 0.0 EPC -> KILL."""
    assert classify_epc(0.0) == "KILL"


def test_classify_negative_epc_is_kill_defensively():
    """Negative EPC is nonsense but must not crash; treat as KILL."""
    assert classify_epc(-0.5) == "KILL"


def test_classify_large_negative_epc_is_kill():
    assert classify_epc(-1_000.0) == "KILL"


# ---------------------------------------------------------------------------
# High-EPC SCALE tier
# ---------------------------------------------------------------------------

def test_classify_very_high_epc_is_scale():
    """RM 5.00 EPC -> SCALE (very profitable campaign)."""
    assert classify_epc(5.0) == "SCALE"


def test_classify_above_one_epc_is_scale():
    """Some campaigns cross RM1 EPC — still SCALE."""
    assert classify_epc(1.25) == "SCALE"


# ---------------------------------------------------------------------------
# Continuous-zone sanity sweep
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "epc,expected",
    [
        (0.0, "KILL"),
        (0.005, "KILL"),
        (0.009, "KILL"),
        (0.01, "HOLD"),       # boundary
        (0.02, "HOLD"),
        (0.03, "HOLD"),
        (0.04, "HOLD"),
        (0.049, "HOLD"),
        (0.05, "SCALE"),      # boundary
        (0.06, "SCALE"),
        (0.10, "SCALE"),
        (1.00, "SCALE"),
        (10.0, "SCALE"),
    ],
)
def test_classify_zones(epc: float, expected: str):
    """Sweep across the three zones to confirm clean transitions."""
    assert classify_epc(epc) == expected


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

def test_classify_returns_only_known_labels():
    """Whatever the input, output must be in CLASSIFICATIONS."""
    for epc in [-1.0, 0.0, 0.001, 0.01, 0.02, 0.05, 0.10, 1.0, 100.0]:
        assert classify_epc(epc) in CLASSIFICATIONS


def test_classify_is_deterministic():
    """Same input -> same output, always."""
    assert classify_epc(0.03) == classify_epc(0.03)
    assert classify_epc(0.03) == "HOLD"


def test_classify_is_pure():
    """Calling classify_epc multiple times must not have side effects."""
    results = [classify_epc(0.03) for _ in range(50)]
    assert all(r == "HOLD" for r in results)


# ---------------------------------------------------------------------------
# classify_many()
# ---------------------------------------------------------------------------

def test_classify_many_empty_returns_empty_list():
    assert classify_many([]) == []


def test_classify_many_preserves_order_and_length():
    epcs = [0.005, 0.03, 0.06, 0.0, 0.05, 0.01]
    assert classify_many(epcs) == ["KILL", "HOLD", "SCALE", "KILL", "SCALE", "HOLD"]


def test_classify_many_returns_list_of_str():
    result = classify_many([0.01, 0.05, 0.10])
    assert isinstance(result, list)
    assert all(isinstance(v, str) for v in result)
