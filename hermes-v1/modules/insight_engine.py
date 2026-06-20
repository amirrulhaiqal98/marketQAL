"""
Insight Engine — turns raw KPIs into SCALE / HOLD / KILL verdicts.

The engine has three layers:

1. :func:`classify_epc`  — pure EPC -> label mapping (single source of truth
   is :mod:`modules.constants`). Unit-tested in isolation.

2. :func:`build_kpis`    — joins Meta + Shopee data by ``campaign_key`` and
   produces a :class:`~modules.models.CampaignKPI` per campaign. Meta data
   is summarized by ad-set name (``SIFAT``, ``PRODUK``) and mapped to the
   Shopee ``campaign_key`` (``FB``, ``produkFB``) via a user-overridable
   :data:`_DEFAULT_CAMPAIGN_MAP`.

3. :func:`render_summary` — formats the KPI list as a Telegram-friendly
   plain-text report. SCALE first, then HOLD, then KILL.

Design rules:

* :func:`classify_epc` and :func:`build_kpis` are pure (no I/O).
* :func:`render_summary` is pure (string in, string out).
* No LLM calls in Phase 1. The qualitative summary is produced by the
  numeric pipeline only; the optional MiniMax ``INSIGHT_PROMPT`` (Step 14)
  is bolted on by ``bot.py``, not here.
* Missing Meta data is rendered as ``"—"``, not omitted, so the user can
  immediately see which campaigns lacked Meta attribution.
"""

from __future__ import annotations

from typing import Optional

from modules.constants import (
    CLASSIFICATIONS,
    DIRECT_TRAFFIC_KEY,
    EPC_KILL_THRESHOLD,
    EPC_SCALE_THRESHOLD,
)
from modules.epc_calculator import compute as _compute_epc
from modules.models import (
    CampaignKPI,
    MetaAdRow,
    ShopeeClickRow,
    ShopeeCommissionRow,
)
from modules.shopee_analyzer import aggregate_by_campaign as _shopee_aggregate


__all__ = [
    "classify_epc",
    "classify_many",
    "build_kpis",
    "render_summary",
    "DEFAULT_CAMPAIGN_MAP",
]


# ---------------------------------------------------------------------------
# Default Meta ad-set -> Shopee campaign_key mapping.
#
# The user's Meta account (per fixture + brainstorming doc) has two ad-sets:
#
#   * "SIFAT"   — curated brand-voice / lifestyle ads   -> Shopee "FB"
#   * "PRODUK"  — product-specific ads                    -> Shopee "produkFB"
#
# This mapping is overridable via the ``campaign_map`` kwarg of
# :func:`build_kpis` so other Meta account structures don't need a code
# change. Ad-sets not in the map appear in the report under their raw
# ad-set name (defensive: don't silently drop Meta spend).
# ---------------------------------------------------------------------------

DEFAULT_CAMPAIGN_MAP: dict[str, str] = {
    "SIFAT": "FB",
    "PRODUK": "produkFB",
}


# Tier ordering used by :func:`render_summary`. SCALE first (best), then
# HOLD (watch), then KILL (worst). Exported as a tuple so tests can assert
# against the canonical order without hard-coding the strings.
_TIER_ORDER: dict[str, int] = {"SCALE": 0, "HOLD": 1, "KILL": 2}
_TIER_MARKER: dict[str, str] = {"SCALE": "🟢", "HOLD": "🟡", "KILL": "🔴"}
_TIER_HEADER: dict[str, str] = {
    "SCALE": "SCALE — profitable, double down",
    "HOLD": "HOLD — break-even, gather data",
    "KILL": "KILL — burning cash, pause now",
}


# ---------------------------------------------------------------------------
# Re-export so downstream callers only need to import insight_engine
# ---------------------------------------------------------------------------

# (move __all__ after DEFAULT_CAMPAIGN_MAP so the constant is importable)


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


# ---------------------------------------------------------------------------
# End-to-end KPI builder
# ---------------------------------------------------------------------------

def build_kpis(
    meta_rows: list[MetaAdRow],
    click_rows: list[ShopeeClickRow],
    commission_rows: list[ShopeeCommissionRow],
    *,
    campaign_map: Optional[dict[str, str]] = None,
) -> list[CampaignKPI]:
    """Join Meta + Shopee data into per-campaign :class:`CampaignKPI` snapshots.

    The pipeline:

    1. Aggregate Shopee clicks + realized commissions via
       :func:`modules.shopee_analyzer.aggregate_by_campaign` (grouped by
       ``campaign_key``: ``"FB"``, ``"produkFB"``, ``"DIRECT"``).
    2. Aggregate Meta rows via
       :func:`modules.meta_analyzer.summarize_campaign` (grouped by
       ``nama_set_iklan``: ``"SIFAT"``, ``"PRODUK"``, ...).
    3. Map each Meta ad-set to a Shopee ``campaign_key`` via
       :data:`DEFAULT_CAMPAIGN_MAP` (or the caller-supplied ``campaign_map``).
       Ad-sets missing from the map are kept under their raw ad-set name so
       Meta spend is never silently dropped.
    4. For each unique ``campaign_key``, emit a :class:`CampaignKPI` with
       Shopee fields (always present) and Meta fields (``None`` if no Meta
       ad-set maps to that key).
    5. Apply :func:`classify_epc` to each KPI's ``epc`` to fill
       ``classification``.

    Args:
        meta_rows: List of :class:`~modules.models.MetaAdRow`. May be empty
            (e.g. user skipped Meta upload) — Meta fields will be ``None``.
        click_rows: List of :class:`~modules.models.ShopeeClickRow`. May be
            empty — every KPI will have ``total_clicks == 0`` and ``epc == 0.0``.
        commission_rows: List of :class:`~modules.models.ShopeeCommissionRow`.
            Pending rows are excluded from commission totals (by
            :func:`aggregate_by_campaign`).
        campaign_map: Optional override of :data:`DEFAULT_CAMPAIGN_MAP`.
            Ad-sets not in the map pass through unchanged.

    Returns:
        A list of :class:`CampaignKPI`, one per ``campaign_key``. Insertion
        order: Shopee-driven campaigns first (in Shopee aggregate order),
        then Meta-only campaigns (in Meta ad-set order).

    Examples:
        >>> kpis = build_kpis(meta, clicks, commissions)
        >>> sorted(k.classification for k in kpis)
        ['HOLD', 'KILL', 'SCALE']
    """
    effective_map: dict[str, str] = dict(campaign_map if campaign_map is not None else DEFAULT_CAMPAIGN_MAP)

    shopee_summary = _shopee_aggregate(click_rows, commission_rows)
    meta_summary = _summarize_meta(meta_rows)

    # Map each Meta ad-set to its Shopee campaign_key. Ad-sets not in the
    # map keep their raw ad-set name (defensive: never silently drop spend).
    meta_campaign_keys: dict[str, str] = {
        ad_set: effective_map.get(ad_set, ad_set) for ad_set in meta_summary
    }

    kpis_by_key: dict[str, CampaignKPI] = {}

    # Pass 1: Shopee-driven KPIs (always present, even with zero clicks).
    for campaign_key, bucket in shopee_summary.items():
        clicks = int(bucket["total_clicks"])
        commission = float(bucket["total_commission_rm"])
        epc = _compute_epc(clicks, commission)
        channels: dict[str, int] = bucket.get("channels", {})  # type: ignore[assignment]
        kpis_by_key[campaign_key] = CampaignKPI(
            campaign_key=campaign_key,
            channel=_top_channel(channels),
            total_clicks=clicks,
            total_commission_rm=round(commission, 2),
            epc=epc,
            classification=classify_epc(epc),
        )

    # Pass 2: Meta-driven join. Always overwrite the Meta fields (they're
    # ``None`` by default), so a Shopee-only campaign stays Shopee-only and
    # a Meta-only campaign appears as a new entry.
    for ad_set, meta_kpis in meta_summary.items():
        campaign_key = meta_campaign_keys[ad_set]
        kpi = kpis_by_key.get(campaign_key) or CampaignKPI(
            campaign_key=campaign_key,
            channel=ad_set,
            total_clicks=0,
            total_commission_rm=0.0,
            epc=0.0,
            classification=classify_epc(0.0),
        )
        kpi.meta_spend_rm = round(float(meta_kpis["total_spend"]), 2)
        kpi.meta_link_clicks = int(meta_kpis["total_link_clicks"])
        kpi.meta_ctr = round(float(meta_kpis["weighted_ctr"]), 6)
        kpi.meta_cpc = round(float(meta_kpis["weighted_cpc"]), 6)
        kpis_by_key[campaign_key] = kpi

    return list(kpis_by_key.values())


# ---------------------------------------------------------------------------
# Telegram-friendly text rendering
# ---------------------------------------------------------------------------

def render_summary(kpis: list[CampaignKPI]) -> str:
    """Render a list of :class:`CampaignKPI` as a Telegram-friendly report.

    The output is plain text (no Markdown) so it's safe to paste into any
    chat client. The bot may post-process it to MarkdownV2 in a later step
    if needed; for Phase 1 we keep the format explicit and human-readable.

    Layout:

    * Header with title.
    * Three sections — SCALE, HOLD, KILL — in that order, each with a
      one-line verdict caption. Empty sections are still rendered (with a
      ``"(none)"`` marker) so the user always sees all three tiers.
    * Per-campaign block: campaign key, clicks, commission (RM), EPC, and
      Meta fields (spend / CTR / CPC) — or ``"—"`` if Meta was not joined.
    * Footer with the total count and a tier breakdown.

    Args:
        kpis: List of :class:`CampaignKPI` from :func:`build_kpis`. May be
            empty (e.g. user uploaded nothing yet) — a "no data" message is
            returned.

    Returns:
        A multi-line plain-text report. Safe for Telegram messages up to
        4096 chars; with realistic data (<= 10 campaigns) the output is
        well under that.
    """
    if not kpis:
        return (
            "📊 Hermes Traffic Intelligence Report\n"
            "══════════════════════════════════════\n"
            "\n"
            "No campaigns to analyze yet.\n"
            "Upload a Meta CSV via /analyze_meta\n"
            "and Shopee CSVs via /analyze_shopee,\n"
            "then run /insights."
        )

    # Sort: SCALE first (best), then HOLD, then KILL, then alphabetical
    # within a tier for deterministic output.
    sorted_kpis = sorted(
        kpis,
        key=lambda k: (_TIER_ORDER.get(k.classification, 99), k.campaign_key),
    )

    counts: dict[str, int] = {tier: 0 for tier in CLASSIFICATIONS}
    for k in sorted_kpis:
        counts[k.classification] = counts.get(k.classification, 0) + 1

    lines: list[str] = []
    lines.append("📊 Hermes Traffic Intelligence Report")
    lines.append("══════════════════════════════════════")
    lines.append("")

    for tier in ("SCALE", "HOLD", "KILL"):
        marker = _TIER_MARKER[tier]
        header = _TIER_HEADER[tier]
        tier_kpis = [k for k in sorted_kpis if k.classification == tier]
        lines.append(f"{marker} {header} ({counts[tier]}):")
        if not tier_kpis:
            lines.append("   (none)")
        else:
            for kpi in tier_kpis:
                lines.extend(_format_kpi_block(kpi))
        lines.append("")

    total = len(sorted_kpis)
    summary_line = (
        f"Total: {total} campaign{'s' if total != 1 else ''} analyzed "
        f"— SCALE: {counts['SCALE']}, "
        f"HOLD: {counts['HOLD']}, "
        f"KILL: {counts['KILL']}"
    )
    lines.append(summary_line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _summarize_meta(
    meta_rows: list[MetaAdRow],
) -> dict[str, dict[str, float | int]]:
    """Aggregate Meta rows by ``nama_set_iklan`` — same math as
    :func:`modules.meta_analyzer.summarize_campaign`, inlined here to keep
    :mod:`insight_engine` decoupled from :mod:`meta_analyzer`'s import path.

    CTR and CPC are weighted (pooled), not averaged — averaging across
    creatives would give equal weight to a 100-impression ad and a
    10,000-impression ad, which is misleading for buying decisions.
    """
    summary: dict[str, dict[str, float | int]] = {}
    for row in meta_rows:
        bucket = summary.setdefault(
            row.nama_set_iklan,
            {
                "total_spend": 0.0,
                "total_link_clicks": 0,
                "total_impressions": 0,
                "ad_count": 0,
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


def _top_channel(channels: dict[str, int]) -> str:
    """Pick the most-clicked referrer; ties broken by first-seen order.

    Returns ``"—"`` if ``channels`` is empty (e.g. commission-only campaign
    with no click attribution).
    """
    if not channels:
        return "—"
    best = next(iter(channels))
    for ref in channels:
        if channels[ref] > channels[best]:
            best = ref
    return best


def _format_kpi_block(kpi: CampaignKPI) -> list[str]:
    """Render one :class:`CampaignKPI` as a multi-line indented block.

    Output (4-space indent under the bullet):

        • FB
            Clicks:        14
            Commission:    RM 26.54
            EPC:           RM 1.8957 / click
            Meta spend:    RM 56.51
            Meta CTR:      1.72%
            Meta CPC:      RM 0.1119
            Top channel:   Facebook

    Meta fields render as ``"—"`` when ``None`` so the user can spot a
    missing Meta upload at a glance.
    """
    lines: list[str] = []
    lines.append(f"  • {kpi.campaign_key}")
    lines.append(f"      Clicks:        {kpi.total_clicks}")
    lines.append(
        f"      Commission:    RM {kpi.total_commission_rm:.2f}"
    )
    lines.append(
        f"      EPC:           RM {kpi.epc:.4f} / click"
    )
    lines.append(
        f"      Meta spend:    {_fmt_money(kpi.meta_spend_rm)}"
    )
    lines.append(
        f"      Meta CTR:      {_fmt_pct(kpi.meta_ctr)}"
    )
    lines.append(
        f"      Meta CPC:      {_fmt_money(kpi.meta_cpc)}"
    )
    lines.append(
        f"      Top channel:   {kpi.channel}"
    )
    return lines


def _fmt_money(value: Optional[float]) -> str:
    """Format a MYR amount with 2 decimals; ``None`` -> ``"—"``."""
    if value is None:
        return "—"
    return f"RM {value:.2f}"


def _fmt_pct(value: Optional[float]) -> str:
    """Format a fraction as a percent with 2 decimals; ``None`` -> ``"—"``."""
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"
