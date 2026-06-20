"""
Meta Ads analyzer — reads Meta (Facebook) Ads Manager CSV exports.

Meta exports ship with Bahasa Malaysia headers (``Nama iklan``, ``Hasil``,
``Teraan`` ...). We normalize those headers to snake_case, parse the dates,
and compute the three derived KPIs every Meta row needs before the
downstream join with Shopee data:

    link_clicks = hasil          (only when result_indicator == "actions:link_click")
    ctr         = link_clicks / teraan        (0.0 if teraan == 0)
    cpc         = jumlah_dibelanjakan / link_clicks   (0.0 if link_clicks == 0)

This module is pure pandas + stdlib. No Telegram, no MiniMax, no network.
The output ``list[MetaAdRow]`` is what the insight engine joins with Shopee
data in Step 10.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from modules.models import MetaAdRow


__all__ = [
    "parse_meta_csv",
    "summarize_campaign",
    "best_creative",
    "worst_creative",
]


# ---------------------------------------------------------------------------
# Header normalization
# ---------------------------------------------------------------------------

# Single source of truth: (Bahasa Malaysia CSV header, snake_case field).
# The lookup map below is derived from this tuple so the original-case
# header is always available for error messages. Derived fields (link_clicks,
# ctr, cpc) are computed in ``_build_meta_ad_row`` and are NOT in this list.
_META_HEADERS: tuple[tuple[str, str], ...] = (
    ("Nama iklan", "nama_iklan"),
    ("Nama set iklan", "nama_set_iklan"),
    ("Jenis bajet", "jenis_bajet"),
    ("Tarikh mula", "tarikh_mula"),
    ("Tarikh tamat", "tarikh_tamat"),
    ("Hasil", "hasil"),
    ("Result indicator", "result_indicator"),
    ("Kos bagi setiap hasil", "kos_bagi_setiap_hasil"),
    ("Jumlah dibelanjakan (MYR)", "jumlah_dibelanjakan"),
    ("Teraan", "teraan"),
    ("Capaian", "capaian"),
)

# Lowercased-stripped CSV header -> MetaAdRow field name. Built from
# :data:`_META_HEADERS` so there is exactly one source of truth.
_META_HEADER_MAP: dict[str, str] = {
    bahasa.lower(): snake for bahasa, snake in _META_HEADERS
}

# Only the link-click result-indicator is interesting to EPC; for any other
# "Result" column (e.g. ``actions:offsite_conversion``) ``link_clicks`` is 0
# because we can't attribute Shopee clicks to it.  This matches the spec in
# ``models.MetaAdRow``: ``link_clicks`` is an alias for ``hasil`` *iff* the
# row is reporting link clicks.
_LINK_CLICK_INDICATOR: str = "actions:link_click"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_meta_csv(path: str | Path) -> list[MetaAdRow]:
    """Parse a Meta Ads CSV export into a list of ``MetaAdRow``.

    The CSV must have Bahasa Malaysia headers (case-insensitive, surrounding
    whitespace ignored). The function:

    * Normalizes headers via :data:`_META_HEADER_MAP`.
    * Parses ``Tarikh mula`` and ``Tarikh tamat`` as :class:`datetime.date`.
    * Coerces numerics defensively (some Meta exports ship text-formatted
      numbers with commas or ``RM`` prefixes — those are stripped).
    * Computes ``link_clicks``, ``ctr``, ``cpc`` for each row.

    Args:
        path: Filesystem path (str or :class:`Path`) to the CSV.

    Returns:
        A list of ``MetaAdRow`` (empty if the CSV has only a header row).
        Order matches the CSV.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If a required column is missing from the CSV.
    """
    df = pd.read_csv(path)
    df = _normalize_headers(df)
    _validate_required_columns(df)

    rows: list[MetaAdRow] = []
    for _, raw in df.iterrows():
        rows.append(_build_meta_ad_row(raw))
    return rows


def summarize_campaign(
    rows: list[MetaAdRow],
) -> dict[str, dict[str, float | int]]:
    """Aggregate Meta rows by ``nama_set_iklan`` (the CBO ad-set grouping).

    For each ad set, returns a dict of KPIs::

        {
            "SIFAT": {
                "total_spend": 56.51,
                "total_link_clicks": 505,
                "total_impressions": 29403,
                "weighted_ctr": 0.017175...,   # total_link_clicks / total_impressions
                "weighted_cpc": 0.111901...,   # total_spend / total_link_clicks
                "ad_count": 5,
            },
            ...
        }

    CTR and CPC are *weighted* (pooled), not averaged — averaging CTR
    across creatives would give equal weight to a 100-impression ad and a
    10000-impression ad, which is misleading for buying decisions.

    Args:
        rows: List of ``MetaAdRow`` from :func:`parse_meta_csv`.

    Returns:
        Mapping ``ad_set_name -> KPI dict``. Ad sets with zero link clicks
        or zero impressions get ``0.0`` for ``weighted_cpc`` / ``weighted_ctr``
        rather than raising.
    """
    summary: dict[str, dict[str, float | int]] = {}
    for row in rows:
        bucket = summary.setdefault(
            row.nama_set_iklan,
            {
                "total_spend": 0.0,
                "total_link_clicks": 0,
                "total_impressions": 0,
                "ad_count": 0,
                # placeholders; computed below
                "weighted_ctr": 0.0,
                "weighted_cpc": 0.0,
            },
        )
        bucket["total_spend"] += float(row.jumlah_dibelanjakan)
        bucket["total_link_clicks"] += int(row.link_clicks)
        bucket["total_impressions"] += int(row.teraan)
        bucket["ad_count"] += 1

    for bucket in summary.values():
        if bucket["total_impressions"] > 0:
            bucket["weighted_ctr"] = (
                bucket["total_link_clicks"] / bucket["total_impressions"]
            )
        if bucket["total_link_clicks"] > 0:
            bucket["weighted_cpc"] = (
                bucket["total_spend"] / bucket["total_link_clicks"]
            )

    return summary


def best_creative(rows: list[MetaAdRow]) -> MetaAdRow:
    """Return the creative (ad) with the highest CTR.

    Args:
        rows: List of ``MetaAdRow``. Must be non-empty.

    Returns:
        The ``MetaAdRow`` whose ``ctr`` is maximal. Ties broken by
        insertion order (first encountered wins — preserves CSV order).

    Raises:
        ValueError: If ``rows`` is empty.
    """
    if not rows:
        raise ValueError("best_creative requires at least one MetaAdRow")

    best = rows[0]
    for row in rows[1:]:
        if row.ctr > best.ctr:
            best = row
    return best


def worst_creative(rows: list[MetaAdRow]) -> MetaAdRow:
    """Return the creative with the lowest CTR, excluding zero-impression rows.

    Zero-impression rows (``teraan == 0``) have undefined CTR; including them
    would always make them the "worst" and obscure actually-bad creatives
    that *did* get shown but failed to convert.

    Args:
        rows: List of ``MetaAdRow``. Must contain at least one row with
            ``teraan > 0``.

    Returns:
        The ``MetaAdRow`` with the lowest CTR among rows that have at
        least one impression. Ties broken by insertion order.

    Raises:
        ValueError: If no row in ``rows`` has ``teraan > 0``.
    """
    eligible = [r for r in rows if r.teraan > 0]
    if not eligible:
        raise ValueError(
            "worst_creative requires at least one row with teraan > 0"
        )

    worst = eligible[0]
    for row in eligible[1:]:
        if row.ctr < worst.ctr:
            worst = row
    return worst


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _normalize_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase + strip + map Bahasa Malaysia headers to snake_case fields.

    Unknown columns are passed through unchanged so future Meta columns
    don't crash the parser; they just get ignored.
    """
    rename: dict[str, str] = {}
    for col in df.columns:
        key = col.strip().lower()
        if key in _META_HEADER_MAP:
            rename[col] = _META_HEADER_MAP[key]
    return df.rename(columns=rename)


def _validate_required_columns(df: pd.DataFrame) -> None:
    """Raise ``ValueError`` listing every missing required column.

    The error message uses the *original-case* Bahasa Malaysia headers
    (e.g. ``"Nama iklan"``) so a user who pasted a CSV with English
    headers can immediately see what was expected.
    """
    required_snake = {snake for _, snake in _META_HEADERS}
    missing_snake = sorted(required_snake - set(df.columns))
    if missing_snake:
        # Render missing fields with their original-case CSV header for
        # user-friendly error messages (e.g. "Nama iklan").
        missing_display = [
            bahasa for bahasa, snake in _META_HEADERS if snake in missing_snake
        ]
        expected_display = sorted(bahasa for bahasa, _ in _META_HEADERS)
        raise ValueError(
            f"Meta CSV missing required columns: {missing_display}. "
            f"Expected Bahasa Malaysia headers: {expected_display}"
        )


def _build_meta_ad_row(raw: pd.Series) -> MetaAdRow:
    """Build one ``MetaAdRow`` from a normalized pandas Series."""
    nama_iklan = str(raw["nama_iklan"]).strip()
    nama_set_iklan = str(raw["nama_set_iklan"]).strip()
    jenis_bajet = str(raw["jenis_bajet"]).strip()
    tarikh_mula = pd.to_datetime(raw["tarikh_mula"]).date()
    tarikh_tamat = pd.to_datetime(raw["tarikh_tamat"]).date()
    hasil = _coerce_int(raw["hasil"])
    result_indicator = str(raw["result_indicator"]).strip()
    kos_bagi_setiap_hasil = _coerce_float(raw["kos_bagi_setiap_hasil"])
    jumlah_dibelanjakan = _coerce_float(raw["jumlah_dibelanjakan"])
    teraan = _coerce_int(raw["teraan"])
    capaian = _coerce_int(raw["capaian"])

    # Derived KPIs
    link_clicks = hasil if result_indicator == _LINK_CLICK_INDICATOR else 0
    ctr = (link_clicks / teraan) if teraan > 0 else 0.0
    cpc = (jumlah_dibelanjakan / link_clicks) if link_clicks > 0 else 0.0

    return MetaAdRow(
        nama_iklan=nama_iklan,
        nama_set_iklan=nama_set_iklan,
        jenis_bajet=jenis_bajet,
        tarikh_mula=tarikh_mula,
        tarikh_tamat=tarikh_tamat,
        hasil=hasil,
        result_indicator=result_indicator,
        kos_bagi_setiap_hasil=kos_bagi_setiap_hasil,
        jumlah_dibelanjakan=jumlah_dibelanjakan,
        teraan=teraan,
        capaian=capaian,
        link_clicks=link_clicks,
        ctr=ctr,
        cpc=cpc,
    )


def _coerce_int(value: object) -> int:
    """Coerce a CSV cell to ``int``. Strips commas / whitespace; NaN -> 0."""
    if pd.isna(value):
        return 0
    return int(float(str(value).replace(",", "").strip()))


def _coerce_float(value: object) -> float:
    """Coerce a CSV cell to ``float``.

    Strips commas, whitespace, and an optional ``RM`` currency prefix.
    NaN -> 0.0.
    """
    if pd.isna(value):
        return 0.0
    text = str(value).replace(",", "").strip()
    if text.upper().startswith("RM"):
        text = text[2:].strip()
    return float(text)
