"""
Shopee Affiliate analyzer — reads Shopee click and commission CSV exports
and derives ``campaign_key`` per row.

Shopee exports ship with English headers (``Click id``, ``Sub_id``,
``Referrer``, ``Affiliate Net Commission(RM)`` ...). We normalize them to
snake_case, parse datetimes, and compute the per-row derived fields the
insight engine needs:

    click_row.campaign_key        = _derive_campaign_key(click_row.sub_id_raw)
    commission_row.campaign_key   = _derive_campaign_key(commission_row.sub_id1)
    commission_row.is_realized    = (order_status == "Completed")

The campaign-key derivation rule (shared by both row types):

    "FB----"      -> "FB"
    "produkFB----" -> "produkFB"
    "----"        -> "DIRECT"           (empty after stripping)
    ""            -> "DIRECT"

This module is pure pandas + stdlib. No Telegram, no MiniMax, no network.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from modules.constants import DIRECT_TRAFFIC_KEY
from modules.models import ShopeeClickRow, ShopeeCommissionRow


__all__ = [
    "parse_click_csv",
    "parse_commission_csv",
    "aggregate_by_campaign",
    "top_channel",
    "_derive_campaign_key",
]


# ---------------------------------------------------------------------------
# Header normalization
# ---------------------------------------------------------------------------

# Single source of truth: (CSV header, snake_case field).
# The lookup map below is derived so the original-case header is always
# available for error messages. Both "Price(RM)" (Shopee's tight form)
# and "Price (RM)" (with space) are accepted by case-insensitive matching
# via :data:`_CLICK_HEADER_MAP` / :data:`_COMMISSION_HEADER_MAP`.

_CLICK_HEADERS: tuple[tuple[str, str], ...] = (
    ("Click id", "click_id"),
    ("Click Time", "click_time"),
    ("Country", "click_region"),
    ("Sub_id", "sub_id_raw"),
    ("Referrer", "referrer"),
)

_COMMISSION_HEADERS: tuple[tuple[str, str], ...] = (
    ("Order id", "order_id"),
    ("Order Status", "order_status"),
    ("Conversion id", "conversion_id"),
    ("Order Time", "order_time"),
    ("Complete Time", "complete_time"),
    ("Click Time", "click_time"),
    ("Shop Name", "shop_name"),
    ("Item Name", "item_name"),
    ("Price(RM)", "price_rm"),
    ("Price (RM)", "price_rm"),          # tolerant alternative
    ("Qty", "qty"),
    ("Purchase Value(RM)", "purchase_value_rm"),
    ("Purchase Value (RM)", "purchase_value_rm"),  # tolerant alternative
    ("Affiliate Net Commission(RM)", "affiliate_net_commission_rm"),
    ("Affiliate Net Commission (RM)", "affiliate_net_commission_rm"),  # tolerant
    ("Sub_id1", "sub_id1"),
    ("Channel", "channel"),
)

_CLICK_HEADER_MAP: dict[str, str] = {
    header.lower(): snake for header, snake in _CLICK_HEADERS
}

_COMMISSION_HEADER_MAP: dict[str, str] = {
    header.lower(): snake for header, snake in _COMMISSION_HEADERS
}


# ---------------------------------------------------------------------------
# Public API — click parsing
# ---------------------------------------------------------------------------

def parse_click_csv(path: str | Path) -> list[ShopeeClickRow]:
    """Parse a Shopee Website Click Report CSV into a list of ``ShopeeClickRow``.

    Args:
        path: Filesystem path (str or :class:`Path`) to the CSV.

    Returns:
        A list of ``ShopeeClickRow`` (empty if the CSV has only a header row).
        Order matches the CSV.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If a required column is missing from the CSV.
    """
    df = pd.read_csv(path)
    df = _normalize_click_headers(df)
    _validate_click_columns(df)

    rows: list[ShopeeClickRow] = []
    for _, raw in df.iterrows():
        rows.append(_build_click_row(raw))
    return rows


# ---------------------------------------------------------------------------
# Public API — commission parsing
# ---------------------------------------------------------------------------

def parse_commission_csv(path: str | Path) -> list[ShopeeCommissionRow]:
    """Parse a Shopee Affiliate Commission Report CSV.

    Args:
        path: Filesystem path (str or :class:`Path`) to the CSV.

    Returns:
        A list of ``ShopeeCommissionRow`` (empty if the CSV has only a
        header row). Order matches the CSV. ``is_realized`` is ``True``
        iff ``order_status == "Completed"`` — the EPC aggregation in
        :func:`aggregate_by_campaign` excludes unrealized rows from totals.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If a required column is missing from the CSV.
    """
    df = pd.read_csv(path)
    df = _normalize_commission_headers(df)
    _validate_commission_columns(df)

    rows: list[ShopeeCommissionRow] = []
    for _, raw in df.iterrows():
        rows.append(_build_commission_row(raw))
    return rows


# ---------------------------------------------------------------------------
# Public API — aggregation
# ---------------------------------------------------------------------------

def aggregate_by_campaign(
    clicks: list[ShopeeClickRow],
    commissions: list[ShopeeCommissionRow],
) -> dict[str, dict[str, float | int | str]]:
    """Group clicks + realized commissions by ``campaign_key``.

    For each campaign, returns::

        {
            "FB": {
                "total_clicks": 14,
                "total_orders": 6,              # unique Conversion ids
                "total_commission_rm": 26.54,
                "epc": 1.8957...,               # commission / clicks
                "completed_commission_rows": 6, # for sanity-checking
                "channels": {"Facebook": 10, "Instagram": 3, "WhatsApp": 1},
                "regions":  {"Malaysia": 11, ...},
            },
            ...
        }

    Note: ``total_orders`` counts unique ``conversion_id`` values, NOT raw
    commission rows. A multi-item order (e.g. ``260618D4CNWBBW`` with 3
    items) counts as 1 order. The raw row count is exposed as
    ``completed_commission_rows`` for debugging.

    Args:
        clicks: List of ``ShopeeClickRow``.
        commissions: List of ``ShopeeCommissionRow``.

    Returns:
        Mapping ``campaign_key -> KPI dict``. Keys appear in insertion
        order: click-driven campaigns first, then commission-only ones.
    """
    summary: dict[str, dict[str, float | int | str]] = {}

    for row in clicks:
        bucket = summary.setdefault(row.campaign_key, _empty_bucket())
        bucket["total_clicks"] += 1
        bucket["channels"][row.referrer] = bucket["channels"].get(row.referrer, 0) + 1
        bucket["regions"][row.click_region] = bucket["regions"].get(row.click_region, 0) + 1

    seen_conversions: dict[str, set[str]] = {}
    for row in commissions:
        if not row.is_realized:
            # Pending / Cancelled: still record the click attribution but
            # exclude from commission totals.
            summary.setdefault(row.campaign_key, _empty_bucket())
            continue
        bucket = summary.setdefault(row.campaign_key, _empty_bucket())
        bucket["total_commission_rm"] += float(row.affiliate_net_commission_rm)
        bucket["completed_commission_rows"] += 1
        seen_conversions.setdefault(row.campaign_key, set()).add(row.conversion_id)

    for key, bucket in summary.items():
        bucket["total_orders"] = len(seen_conversions.get(key, set()))
        clicks_count = int(bucket["total_clicks"])
        if clicks_count > 0:
            bucket["epc"] = round(
                float(bucket["total_commission_rm"]) / clicks_count, 6
            )

    return summary


def top_channel(clicks: list[ShopeeClickRow]) -> tuple[str, int]:
    """Return the ``(referrer, click_count)`` pair with the most clicks.

    Args:
        clicks: List of ``ShopeeClickRow``. Must be non-empty.

    Returns:
        The most-clicked ``referrer`` and its click count. Ties broken by
        first-encountered order (preserves CSV order).

    Raises:
        ValueError: If ``clicks`` is empty.
    """
    if not clicks:
        raise ValueError("top_channel requires at least one ShopeeClickRow")

    counts: dict[str, int] = {}
    order: list[str] = []
    for row in clicks:
        if row.referrer not in counts:
            counts[row.referrer] = 0
            order.append(row.referrer)
        counts[row.referrer] += 1

    best = order[0]
    for ref in order[1:]:
        if counts[ref] > counts[best]:
            best = ref
    return best, counts[best]


# ---------------------------------------------------------------------------
# Public helpers — campaign_key derivation (also exposed for tests)
# ---------------------------------------------------------------------------

def _derive_campaign_key(sub_id: str) -> str:
    """Derive a campaign key from a Shopee ``Sub_id`` / ``Sub_id1`` value.

    Rule (confirmed from user's real data):
        * Strip trailing dashes (``-`` characters).
        * If the result is empty, return :data:`DIRECT_TRAFFIC_KEY` (``"DIRECT"``).
        * Otherwise return the stripped value verbatim.

    Examples::

        _derive_campaign_key("FB----")         == "FB"
        _derive_campaign_key("produkFB----")   == "produkFB"
        _derive_campaign_key("----")           == "DIRECT"
        _derive_campaign_key("")               == "DIRECT"
        _derive_campaign_key("FB")             == "FB"   # already clean
        _derive_campaign_key(None)             == "DIRECT"  # defensive

    Args:
        sub_id: The raw Sub_id / Sub_id1 cell (str). ``None`` is tolerated.

    Returns:
        The derived campaign key.
    """
    if sub_id is None:
        return DIRECT_TRAFFIC_KEY
    # Order matters: strip surrounding whitespace FIRST (defensive against
    # stray spaces in real Sub_id cells), then strip trailing dashes.
    normalized = sub_id.strip()
    stripped = normalized.rstrip("-")
    return stripped if stripped else DIRECT_TRAFFIC_KEY


# ---------------------------------------------------------------------------
# Internals — header normalization & validation
# ---------------------------------------------------------------------------

def _normalize_click_headers(df: pd.DataFrame) -> pd.DataFrame:
    rename: dict[str, str] = {}
    for col in df.columns:
        key = col.strip().lower()
        if key in _CLICK_HEADER_MAP:
            rename[col] = _CLICK_HEADER_MAP[key]
    return df.rename(columns=rename)


def _normalize_commission_headers(df: pd.DataFrame) -> pd.DataFrame:
    rename: dict[str, str] = {}
    for col in df.columns:
        key = col.strip().lower()
        if key in _COMMISSION_HEADER_MAP:
            rename[col] = _COMMISSION_HEADER_MAP[key]
    return df.rename(columns=rename)


def _validate_click_columns(df: pd.DataFrame) -> None:
    required_snake = {snake for _, snake in _CLICK_HEADERS}
    missing_snake = sorted(required_snake - set(df.columns))
    if missing_snake:
        missing_display = [
            header for header, snake in _CLICK_HEADERS if snake in missing_snake
        ]
        expected_display = sorted(header for header, _ in _CLICK_HEADERS)
        raise ValueError(
            f"Shopee click CSV missing required columns: {missing_display}. "
            f"Expected headers: {expected_display}"
        )


def _validate_commission_columns(df: pd.DataFrame) -> None:
    # Filter tolerant duplicates: if a snake_case field is mapped from more
    # than one CSV header (e.g. "Price(RM)" and "Price (RM)"), only require
    # it once.
    required_snake: set[str] = set()
    for _, snake in _COMMISSION_HEADERS:
        required_snake.add(snake)
    missing_snake = sorted(required_snake - set(df.columns))
    if missing_snake:
        missing_display = [
            header for header, snake in _COMMISSION_HEADERS if snake in missing_snake
        ]
        expected_display = sorted(set(header for header, _ in _COMMISSION_HEADERS))
        raise ValueError(
            f"Shopee commission CSV missing required columns: {missing_display}. "
            f"Expected headers: {expected_display}"
        )


# ---------------------------------------------------------------------------
# Internals — row builders
# ---------------------------------------------------------------------------

def _build_click_row(raw: pd.Series) -> ShopeeClickRow:
    sub_id_raw = "" if pd.isna(raw["sub_id_raw"]) else str(raw["sub_id_raw"]).strip()
    return ShopeeClickRow(
        click_id=str(raw["click_id"]).strip(),
        click_time=pd.to_datetime(raw["click_time"]).to_pydatetime(),
        click_region=str(raw["click_region"]).strip(),
        sub_id_raw=sub_id_raw,
        referrer=str(raw["referrer"]).strip(),
        campaign_key=_derive_campaign_key(sub_id_raw),
    )


def _build_commission_row(raw: pd.Series) -> ShopeeCommissionRow:
    sub_id1 = "" if pd.isna(raw["sub_id1"]) else str(raw["sub_id1"]).strip()
    order_status = str(raw["order_status"]).strip()
    return ShopeeCommissionRow(
        order_id=str(raw["order_id"]).strip(),
        order_status=order_status,
        conversion_id=str(raw["conversion_id"]).strip(),
        order_time=_optional_datetime(raw["order_time"]),
        complete_time=_optional_datetime(raw["complete_time"]),
        click_time=pd.to_datetime(raw["click_time"]).to_pydatetime(),
        shop_name=str(raw["shop_name"]).strip(),
        item_name=str(raw["item_name"]).strip(),
        price_rm=_coerce_float(raw["price_rm"]),
        qty=_coerce_int(raw["qty"]),
        purchase_value_rm=_coerce_float(raw["purchase_value_rm"]),
        affiliate_net_commission_rm=_coerce_float(raw["affiliate_net_commission_rm"]),
        sub_id1=sub_id1,
        channel=str(raw["channel"]).strip(),
        campaign_key=_derive_campaign_key(sub_id1),
        is_realized=(order_status == "Completed"),
    )


def _empty_bucket() -> dict[str, float | int | str | dict[str, int]]:
    """Fresh KPI bucket for :func:`aggregate_by_campaign`."""
    return {
        "total_clicks": 0,
        "total_orders": 0,
        "total_commission_rm": 0.0,
        "epc": 0.0,
        "completed_commission_rows": 0,
        "channels": {},
        "regions": {},
    }


def _coerce_int(value: object) -> int:
    if pd.isna(value):
        return 0
    return int(float(str(value).replace(",", "").strip()))


def _coerce_float(value: object) -> float:
    if pd.isna(value):
        return 0.0
    text = str(value).replace(",", "").strip()
    return float(text)


def _optional_datetime(value: object):
    """Parse a datetime cell that may be empty / ``NaN``.

    Returns ``None`` for empty cells (Pending orders have no ``Complete Time``
    and sometimes no ``Order Time``). Otherwise returns a :class:`datetime`.
    """
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    return pd.to_datetime(text).to_pydatetime()
