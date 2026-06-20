"""
Unit tests for modules.epc_calculator.

Pure tests — no fixtures, no I/O, no async. These run in <10ms.
"""

from __future__ import annotations

import math

import pytest

from modules.epc_calculator import EPC_DECIMALS, compute, compute_bulk


# ---------------------------------------------------------------------------
# compute() — happy path
# ---------------------------------------------------------------------------

def test_compute_brainstorming_example_matches_users_data():
    """The exact example from brainstorming-1.0.md:
        251 clicks, RM 6.34 commission  ->  RM 0.025 EPC
    """
    result = compute(251, 6.34)
    # 6.34 / 251 = 0.0252589641434... -> rounded to 6dp = 0.025259
    assert result == pytest.approx(0.025259, abs=1e-6)


def test_compute_returns_finite_float_always():
    """EPC must never be NaN or infinity — downstream Telegram code relies on this."""
    result = compute(0, 100.0)
    assert math.isfinite(result)
    assert result == 0.0


def test_compute_simple_round_number():
    """100 clicks, RM10 commission -> RM0.10 EPC, exact."""
    assert compute(100, 10.0) == 0.1


def test_compute_large_clicks_small_commission():
    """High-volume, low-commission scenario — must scale down cleanly."""
    # 100_000 clicks, RM 23.50 -> 0.000235
    assert compute(100_000, 23.50) == pytest.approx(0.000235, abs=1e-6)


def test_compute_large_commission_small_clicks():
    """A SCALE-tier campaign: 10 clicks, RM 5.00 commission -> RM 0.50 EPC."""
    assert compute(10, 5.0) == 0.5


# ---------------------------------------------------------------------------
# compute() — edge cases (from implementation_plan.md §[Testing])
# ---------------------------------------------------------------------------

def test_compute_zero_clicks_returns_zero():
    """Edge case #1: clicks=0 must return 0.0, not raise."""
    assert compute(0, 5.0) == 0.0


def test_compute_zero_commission_returns_zero():
    """No money earned -> EPC is exactly 0.0."""
    assert compute(500, 0.0) == 0.0


def test_compute_negative_clicks_treated_as_zero():
    """Defensive: malformed data should not produce negative EPC."""
    assert compute(-10, 5.0) == 0.0


def test_compute_decimal_precision_is_bounded():
    """Result must be rounded to at most EPC_DECIMALS decimal places."""
    # 6.34 / 7 = 0.9057142857142857 -> rounded to 6dp = 0.905714
    result = compute(7, 6.34)
    assert result == 0.905714


def test_compute_threshold_boundary_scale():
    """EPC exactly at SCALE threshold (0.05)."""
    # 100 clicks, RM5 -> 0.05
    assert compute(100, 5.0) == 0.05


def test_compute_threshold_boundary_kill():
    """EPC just below KILL threshold (0.01)."""
    # 1000 clicks, RM9.99 -> 0.00999
    result = compute(1000, 9.99)
    assert result == 0.00999


# ---------------------------------------------------------------------------
# compute_bulk()
# ---------------------------------------------------------------------------

def test_compute_bulk_empty_returns_empty_list():
    assert compute_bulk([]) == []


def test_compute_bulk_preserves_order():
    rows = [
        (251, 6.34),    # 0.025259
        (100, 10.0),    # 0.1
        (0, 5.0),       # 0.0 (div-by-zero)
        (10, 5.0),      # 0.5
    ]
    result = compute_bulk(rows)
    assert result == pytest.approx([0.025259, 0.1, 0.0, 0.5], abs=1e-6)


def test_compute_bulk_returns_list_of_floats():
    result = compute_bulk([(50, 1.0), (100, 2.5)])
    assert isinstance(result, list)
    assert all(isinstance(v, float) for v in result)


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------

def test_epc_decimals_constant_is_positive_int():
    assert isinstance(EPC_DECIMALS, int)
    assert EPC_DECIMALS >= 0


def test_compute_is_pure():
    """Calling compute twice with the same args must yield identical results."""
    a = compute(123, 4.56)
    b = compute(123, 4.56)
    assert a == b
