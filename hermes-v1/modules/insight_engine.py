"""
Insight Engine — turns raw KPIs into SCALE / HOLD / KILL verdicts.

This module owns the *classification* of a single campaign's EPC into one
of three actionable buckets. The end-to-end ``build_kpis`` + ``render_summary``
pipeline lands in Step 10 once the Meta + Shopee analyzers exist; for now
we ship the pure :func:`classify_epc` so the threshold logic is unit-tested
in isolation before any I/O is bolted on.
"""

from __future__ import annotations

from modules.constants import (
    CLASSIFICATIONS,
    EPC_KILL_THRESHOLD,
    EPC_SCALE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Re-export so downstream callers only need to import insight_engine
# ---------------------------------------------------------------------------

__all__ = ["classify_epc", "classify_many"]


# ---------------------------------------------------------------------------
# Core classifier
# ---------------------------------------------------------------------------

def classify_epc(epc: float) -> str:
    """Classify an Earnings-Per-Click value into a traffic verdict.

    Decision rule (matches ``constants.py`` thresholds, which are the single
    source of truth):

    * ``epc >= EPC_SCALE_THRESHOLD``  -> ``"SCALE"``  (campaign pays for itself + margin)
    * ``epc <  EPC_KILL_THRESHOLD``  -> ``"KILL"``   (campaign burns money)
    * otherwise                      -> ``"HOLD"``   (insufficient data / break-even)

    Default thresholds: SCALE >= 0.05 RM/click, KILL < 0.01 RM/click.

    Args:
        epc: Earnings-per-click value in RM. May be 0.0 (from a zero-click
            campaign — see :func:`modules.epc_calculator.compute`). Negative
            values are treated as KILL defensively.

    Returns:
        One of ``"SCALE"``, ``"HOLD"``, ``"KILL"``.

    Examples:
        >>> classify_epc(0.06)
        'SCALE'
        >>> classify_epc(0.03)
        'HOLD'
        >>> classify_epc(0.005)
        'KILL'
    """
    # Defensive: malformed data must never crash the insight summary.
    if epc < 0:
        return "KILL"

    if epc >= EPC_SCALE_THRESHOLD:
        return "SCALE"
    if epc < EPC_KILL_THRESHOLD:
        return "KILL"
    return "HOLD"


def classify_many(epcs: list[float]) -> list[str]:
    """Apply :func:`classify_epc` to a batch of EPC values.

    Returns a list of verdicts in the same order as the input.
    """
    return [classify_epc(epc) for epc in epcs]
