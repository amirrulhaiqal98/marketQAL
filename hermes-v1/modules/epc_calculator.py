"""
EPC (Earnings Per Click) calculator — pure functions, no I/O.

EPC answers the only question that matters to an affiliate traffic buyer:

    "For every click this campaign generates, how much realized commission
    does it earn back, in Ringgit Malaysia?"

Formula:
    EPC = Realized Commission (RM) / Total Clicks

If clicks == 0 we return 0.0 (never raise, never return NaN/inf — Telegram
handlers and the insight engine rely on a finite float).
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Number of decimal places EPC values are rounded to. Six is enough to
#: distinguish RM0.000001 between two campaigns and survives float-printing.
EPC_DECIMALS: int = 6


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def compute(clicks: int, commission_rm: float) -> float:
    """Compute Earnings Per Click in RM.

    Args:
        clicks: Total click count for the campaign. Must be >= 0.
        commission_rm: Realized commission in Ringgit Malaysia (Completed
            orders only). Must be >= 0.

    Returns:
        EPC value rounded to 6 decimal places. Returns ``0.0`` when
        ``clicks == 0`` to avoid a division-by-zero — the caller does
        not need to special-case it.

    Examples:
        >>> compute(251, 6.34)
        0.025259
        >>> compute(0, 5.0)
        0.0
        >>> compute(1000, 0)
        0.0
    """
    if clicks <= 0:
        return 0.0

    epc = commission_rm / clicks
    return round(epc, EPC_DECIMALS)


def compute_bulk(rows: list[tuple[int, float]]) -> list[float]:
    """Vectorised wrapper over :func:`compute`.

    Args:
        rows: Iterable of ``(clicks, commission_rm)`` tuples.

    Returns:
        A list of EPC floats in the same order as the input. An empty
        input yields an empty list.
    """
    return [compute(clicks, commission_rm) for clicks, commission_rm in rows]
