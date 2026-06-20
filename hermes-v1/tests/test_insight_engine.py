"""
Unit tests for the end-to-end Insight Engine pipeline.

Covers two layers:

1. :func:`modules.insight_engine.build_kpis` — joins Meta + Shopee data
   by ``campaign_key`` and produces :class:`~modules.models.CampaignKPI`
   snapshots with classifications.
2. :func:`modules.insight_engine.render_summary` — formats the KPI list as
   a Telegram-friendly plain-text report.

The integration test at the bottom loads the real CSV fixtures and
verifies that the math matches the brainstorming example end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modules.constants import (
    CLASSIFICATIONS,
    EPC_KILL_THRESHOLD,
    EPC_SCALE_THRESHOLD,
)
from modules.insight_engine import (
    DEFAULT_CAMPAIGN_MAP,
    build_kpis,
    classify_epc,
    render_summary,
)
from modules.meta_analyzer import parse_meta_csv
from modules.models import (
    CampaignKPI,
    MetaAdRow,
    ShopeeClickRow,
    ShopeeCommissionRow,
)
from modules.shopee_analyzer import (
    parse_click_csv,
    parse_commission_csv,
)


# Self-contained fixtures path: tests/<file>.py -> tests/ -> hermes-v1/
# -> data/fixtures/. Defined here so the integration test is portable
# without a shared conftest constant.
FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures"


# ---------------------------------------------------------------------------
# Tiny in-memory factories — keep unit tests free of CSV I/O
# ---------------------------------------------------------------------------

def _meta_row(
    nama_set_iklan: str = "SIFAT",
    *,
    spend: float = 10.0,
    link_clicks: int = 50,
    impressions: int = 5000,
    nama_iklan: str = "VIDEO TEST",
    result_indicator: str = "actions:link_click",
) -> MetaAdRow:
    """Hand-craft one MetaAdRow with sane defaults for unit tests."""
    ctr = (link_clicks / impressions) if impressions else 0.0
    cpc = (spend / link_clicks) if link_clicks else 0.0
    from datetime import date
    return MetaAdRow(
        nama_iklan=nama_iklan,
        nama_set_iklan=nama_set_iklan,
        jenis_bajet="Using campaign budget",
        tarikh_mula=date(2026, 6, 14),
        tarikh_tamat=date(2026, 6, 18),
        hasil=link_clicks,
        result_indicator=result_indicator,
        kos_bagi_setiap_hasil=cpc,
        jumlah_dibelanjakan=spend,
        teraan=impressions,
        capaian=int(impressions * 0.95),
        link_clicks=link_clicks,
        ctr=ctr,
        cpc=cpc,
    )


def _click_row(
    campaign_key: str = "FB",
    *,
    referrer: str = "Facebook",
    region: str = "Malaysia",
) -> ShopeeClickRow:
    from datetime import datetime
    sub_id_raw = f"{campaign_key}----" if campaign_key != "DIRECT" else "----"
    # Reuse the real derivation rule so test inputs match production logic.
    from modules.shopee_analyzer import _derive_campaign_key
    return ShopeeClickRow(
        click_id="CL_TEST",
        click_time=datetime(2026, 6, 18, 9, 0, 0),
        click_region=region,
        sub_id_raw=sub_id_raw,
        referrer=referrer,
        campaign_key=_derive_campaign_key(sub_id_raw),
    )


def _commission_row(
    campaign_key: str = "FB",
    *,
    conversion_id: str = "CNV_TEST",
    status: str = "Completed",
    commission: float = 5.0,
    sub_id1: str = "FB",
) -> ShopeeCommissionRow:
    from datetime import datetime
    return ShopeeCommissionRow(
        order_id="ORD_TEST",
        order_status=status,
        conversion_id=conversion_id,
        order_time=datetime(2026, 6, 18, 10, 0, 0),
        complete_time=datetime(2026, 6, 18, 11, 0, 0) if status == "Completed" else None,
        click_time=datetime(2026, 6, 18, 9, 0, 0),
        shop_name="TestShop",
        item_name="Test Item",
        price_rm=20.0,
        qty=1,
        purchase_value_rm=20.0,
        affiliate_net_commission_rm=commission,
        sub_id1=sub_id1,
        channel=referrer_for(campaign_key),
        campaign_key=campaign_key,
        is_realized=(status == "Completed"),
    )


def referrer_for(campaign_key: str) -> str:
    return {
        "FB": "Facebook",
        "produkFB": "Facebook",
        "DIRECT": "Others",
    }.get(campaign_key, "Others")


# ---------------------------------------------------------------------------
# build_kpis — empty / single-source inputs
# ---------------------------------------------------------------------------

def test_build_kpis_all_empty_returns_empty_list():
    """No data at all -> empty list, not a crash."""
    assert build_kpis([], [], []) == []


def test_build_kpis_meta_only_returns_zero_clicks_kpis():
    """Meta-only: KPI exists with clicks=0, epc=0.0, classification=KILL,
    and Meta fields populated. Shopee fields are zero/empty."""
    rows = [_meta_row("SIFAT", spend=20.0, link_clicks=100, impressions=5000)]
    kpis = build_kpis(rows, [], [])

    assert len(kpis) == 1
    kpi = kpis[0]
    assert kpi.campaign_key == "FB"  # SIFAT -> FB via default map
    assert kpi.total_clicks == 0
    assert kpi.total_commission_rm == 0.0
    assert kpi.epc == 0.0
    assert kpi.classification == "KILL"
    assert kpi.meta_spend_rm == 20.0
    assert kpi.meta_link_clicks == 100


def test_build_kpis_shopee_only_returns_kpis_with_none_meta_fields():
    """Shopee-only: KPI exists with Meta fields = None."""
    clicks = [_click_row("FB"), _click_row("FB")]
    commissions = [_commission_row("FB", commission=10.0)]
    kpis = build_kpis([], clicks, commissions)

    assert len(kpis) == 1
    kpi = kpis[0]
    assert kpi.campaign_key == "FB"
    assert kpi.total_clicks == 2
    assert kpi.total_commission_rm == 10.0
    assert kpi.epc == 5.0  # 10 / 2 -> SCALE
    assert kpi.classification == "SCALE"
    assert kpi.meta_spend_rm is None
    assert kpi.meta_link_clicks is None
    assert kpi.meta_ctr is None
    assert kpi.meta_cpc is None


# ---------------------------------------------------------------------------
# build_kpis — join behaviour
# ---------------------------------------------------------------------------

def test_build_kpis_joins_meta_and_shopee_on_default_map():
    """Default map merges SIFAT-spend with FB-clicks into one KPI."""
    meta = [_meta_row("SIFAT", spend=10.0, link_clicks=100, impressions=5000)]
    clicks = [_click_row("FB"), _click_row("FB"), _click_row("FB")]
    commissions = [_commission_row("FB", commission=15.0)]
    kpis = build_kpis(meta, clicks, commissions)

    assert len(kpis) == 1
    kpi = kpis[0]
    assert kpi.campaign_key == "FB"
    assert kpi.total_clicks == 3
    assert kpi.total_commission_rm == 15.0
    assert kpi.epc == 5.0
    assert kpi.classification == "SCALE"
    assert kpi.meta_spend_rm == 10.0
    assert kpi.meta_link_clicks == 100


def test_build_kpis_custom_campaign_map_overrides_defaults():
    """Custom map wins over :data:`DEFAULT_CAMPAIGN_MAP`."""
    custom = {"SIFAT": "BRAND"}
    meta = [_meta_row("SIFAT", spend=5.0)]
    clicks = [_click_row("FB"), _click_row("FB")]
    commissions = [_commission_row("FB", commission=10.0)]
    kpis = build_kpis(meta, clicks, commissions, campaign_map=custom)

    # BRAND comes from Meta only; FB comes from Shopee only — 2 KPIs.
    keys = sorted(k.campaign_key for k in kpis)
    assert keys == ["BRAND", "FB"]


def test_build_kpis_adset_not_in_map_appears_under_raw_name():
    """An ad-set like 'TEST' that's not in the map keeps its raw name."""
    meta = [_meta_row("TEST", spend=5.0)]
    kpis = build_kpis(meta, [], [])

    assert len(kpis) == 1
    assert kpis[0].campaign_key == "TEST"


# ---------------------------------------------------------------------------
# build_kpis — classification logic
# ---------------------------------------------------------------------------

def test_build_kpis_classification_uses_epc_thresholds():
    """Same threshold rules as :func:`classify_epc`."""
    # 100 clicks, 6.0 commission -> 0.06 EPC -> SCALE
    clicks = [_click_row("FB") for _ in range(100)]
    commissions = [_commission_row("FB", commission=6.0)]
    kpis = build_kpis([], clicks, commissions)
    assert kpis[0].classification == "SCALE"

    # 100 clicks, 3.0 commission -> 0.03 EPC -> HOLD
    commissions = [_commission_row("FB", commission=3.0)]
    kpis = build_kpis([], clicks, commissions)
    assert kpis[0].classification == "HOLD"

    # 100 clicks, 0.5 commission -> 0.005 EPC -> KILL
    commissions = [_commission_row("FB", commission=0.5)]
    kpis = build_kpis([], clicks, commissions)
    assert kpis[0].classification == "KILL"


def test_build_kpis_pending_orders_excluded_from_epc():
    """A Pending commission does NOT contribute to commission totals."""
    clicks = [_click_row("FB") for _ in range(10)]
    commissions = [
        _commission_row("FB", commission=10.0, status="Completed"),
        _commission_row("FB", conversion_id="CNV_OTHER", commission=99.0, status="Pending"),
    ]
    kpis = build_kpis([], clicks, commissions)

    assert len(kpis) == 1
    # Only the Completed 10.0 counts -> 10/10 = 1.0 EPC -> SCALE
    assert kpis[0].total_commission_rm == 10.0
    assert kpis[0].epc == 1.0
    assert kpis[0].classification == "SCALE"


def test_build_kpis_multi_item_order_commission_sums_correctly():
    """A 3-row multi-item order (same conversion_id) sums all commissions."""
    clicks = [_click_row("FB") for _ in range(10)]
    commissions = [
        _commission_row("FB", conversion_id="CNV_MULTI", commission=2.78),
        _commission_row("FB", conversion_id="CNV_MULTI", commission=1.94),
        _commission_row("FB", conversion_id="CNV_MULTI", commission=6.75),
    ]
    kpis = build_kpis([], clicks, commissions)

    assert len(kpis) == 1
    # 2.78 + 1.94 + 6.75 = 11.47
    assert kpis[0].total_commission_rm == 11.47
    assert kpis[0].epc == pytest.approx(1.147, abs=1e-6)


def test_build_kpis_direct_traffic_preserved():
    """DIRECT campaign appears as a separate KPI."""
    meta = [_meta_row("SIFAT"), _meta_row("PRODUK")]
    clicks = [
        _click_row("FB"),
        _click_row("produkFB"),
        _click_row("DIRECT"),
        _click_row("DIRECT"),
    ]
    commissions = [
        _commission_row("FB", commission=5.0),
        _commission_row("produkFB", commission=2.0, sub_id1="produkFB"),
    ]
    kpis = build_kpis(meta, clicks, commissions)

    keys = sorted(k.campaign_key for k in kpis)
    assert keys == ["DIRECT", "FB", "produkFB"]
    direct_kpi = next(k for k in kpis if k.campaign_key == "DIRECT")
    assert direct_kpi.total_clicks == 2
    assert direct_kpi.total_commission_rm == 0.0
    assert direct_kpi.classification == "KILL"
    # DIRECT has no Meta ad-set, so Meta fields are None.
    assert direct_kpi.meta_spend_rm is None


# ---------------------------------------------------------------------------
# build_kpis — Meta CTR / CPC math
# ---------------------------------------------------------------------------

def test_build_kpis_meta_ctr_is_weighted_pooled():
    """Weighted CTR across multiple creatives, not arithmetic mean."""
    meta = [
        _meta_row("SIFAT", nama_iklan="A", link_clicks=100, impressions=10000, spend=10.0),
        _meta_row("SIFAT", nama_iklan="B", link_clicks=200, impressions=5000, spend=20.0),
    ]
    kpis = build_kpis(meta, [], [])

    # total clicks = 300, total impressions = 15000 -> CTR = 0.02
    assert kpis[0].meta_ctr == pytest.approx(0.02, abs=1e-6)
    # total spend = 30, total clicks = 300 -> CPC = 0.10
    assert kpis[0].meta_cpc == pytest.approx(0.10, abs=1e-6)
    assert kpis[0].meta_spend_rm == 30.0
    assert kpis[0].meta_link_clicks == 300


def test_build_kpis_meta_adset_with_zero_clicks_handles_zero_division():
    """Zero-click ad-set: weighted_ctr = 0.0 (impressions > 0), weighted_cpc = 0.0."""
    meta = [_meta_row("SIFAT", link_clicks=0, impressions=1000, spend=5.0)]
    kpis = build_kpis(meta, [], [])

    assert kpis[0].meta_ctr == 0.0
    assert kpis[0].meta_cpc == 0.0


# ---------------------------------------------------------------------------
# build_kpis — output type contract
# ---------------------------------------------------------------------------

def test_build_kpis_returns_only_campaign_kpi_instances():
    """Every returned object is a CampaignKPI."""
    kpis = build_kpis(
        [_meta_row("SIFAT")],
        [_click_row("FB")],
        [_commission_row("FB")],
    )
    assert all(isinstance(k, CampaignKPI) for k in kpis)


def test_build_kpis_classifications_in_valid_set():
    """Every classification must be one of SCALE / HOLD / KILL."""
    kpis = build_kpis(
        [_meta_row("SIFAT"), _meta_row("PRODUK")],
        [_click_row("FB"), _click_row("produkFB"), _click_row("DIRECT")],
        [_commission_row("FB", commission=1.0), _commission_row("produkFB", commission=10.0, sub_id1="produkFB")],
    )
    for k in kpis:
        assert k.classification in CLASSIFICATIONS


# ---------------------------------------------------------------------------
# render_summary — empty / header
# ---------------------------------------------------------------------------

def test_render_summary_empty_list_returns_no_data_message():
    """Empty KPI list -> friendly 'upload data' message, no crash."""
    out = render_summary([])
    assert "No campaigns to analyze yet" in out
    assert "/analyze_meta" in out
    assert "/analyze_shopee" in out


def test_render_summary_starts_with_title_and_divider():
    """Every report starts with the title + double-line divider."""
    kpis = [CampaignKPI(campaign_key="FB", channel="Facebook", total_clicks=10,
                        total_commission_rm=5.0, epc=0.5, classification="SCALE")]
    out = render_summary(kpis)
    assert "Hermes Traffic Intelligence Report" in out
    assert "══════" in out  # divider characters present


# ---------------------------------------------------------------------------
# render_summary — ordering, sections, and tier markers
# ---------------------------------------------------------------------------

def test_render_summary_orders_scale_before_hold_before_kill():
    """SCALE campaigns appear above HOLD, HOLD above KILL."""
    kpis = [
        CampaignKPI(campaign_key="Z-KILL", channel="FB", total_clicks=10,
                    total_commission_rm=0.05, epc=0.005, classification="KILL"),
        CampaignKPI(campaign_key="A-SCALE", channel="FB", total_clicks=10,
                    total_commission_rm=10.0, epc=1.0, classification="SCALE"),
        CampaignKPI(campaign_key="M-HOLD", channel="FB", total_clicks=10,
                    total_commission_rm=0.3, epc=0.03, classification="HOLD"),
    ]
    out = render_summary(kpis)
    pos_scale = out.find("A-SCALE")
    pos_hold = out.find("M-HOLD")
    pos_kill = out.find("Z-KILL")
    assert 0 < pos_scale < pos_hold < pos_kill


def test_render_summary_includes_all_three_tier_sections():
    """All three tier sections (SCALE/HOLD/KILL) appear, even if empty."""
    kpis = [CampaignKPI(campaign_key="FB", channel="FB", total_clicks=10,
                        total_commission_rm=10.0, epc=1.0, classification="SCALE")]
    out = render_summary(kpis)
    assert "SCALE" in out
    assert "HOLD" in out
    assert "KILL" in out
    assert "(none)" in out  # empty HOLD/KILL sections marked


def test_render_summary_uses_tier_emoji_markers():
    """🟢 for SCALE, 🟡 for HOLD, 🔴 for KILL."""
    kpis = [
        CampaignKPI(campaign_key="K1", channel="FB", total_clicks=10,
                    total_commission_rm=10.0, epc=1.0, classification="SCALE"),
        CampaignKPI(campaign_key="K2", channel="FB", total_clicks=10,
                    total_commission_rm=0.3, epc=0.03, classification="HOLD"),
        CampaignKPI(campaign_key="K3", channel="FB", total_clicks=10,
                    total_commission_rm=0.05, epc=0.005, classification="KILL"),
    ]
    out = render_summary(kpis)
    assert "🟢" in out
    assert "🟡" in out
    assert "🔴" in out


def test_render_summary_includes_per_campaign_kpi_data():
    """Each campaign block surfaces the campaign_key, clicks, EPC, RM commission."""
    kpis = [CampaignKPI(campaign_key="FB", channel="Facebook", total_clicks=14,
                        total_commission_rm=26.54, epc=1.8957, classification="SCALE")]
    out = render_summary(kpis)
    assert "FB" in out
    assert "Clicks:" in out
    assert "14" in out
    assert "RM 26.54" in out
    assert "RM 1.8957" in out
    assert "EPC:" in out


def test_render_summary_shows_em_dash_for_missing_meta_fields():
    """When a campaign has no Meta join, fields render as '—'."""
    kpis = [CampaignKPI(
        campaign_key="DIRECT", channel="Others", total_clicks=10,
        total_commission_rm=0.0, epc=0.0, classification="KILL",
        # meta_spend_rm / meta_ctr / meta_cpc all default None
    )]
    out = render_summary(kpis)
    assert "Meta spend:    —" in out
    assert "Meta CTR:      —" in out
    assert "Meta CPC:      —" in out


def test_render_summary_footer_includes_tier_counts():
    """Footer line includes per-tier counts and grand total."""
    kpis = [
        CampaignKPI(campaign_key="A", channel="FB", total_clicks=1,
                    total_commission_rm=1.0, epc=1.0, classification="SCALE"),
        CampaignKPI(campaign_key="B", channel="FB", total_clicks=1,
                    total_commission_rm=0.03, epc=0.03, classification="HOLD"),
        CampaignKPI(campaign_key="C", channel="FB", total_clicks=1,
                    total_commission_rm=0.005, epc=0.005, classification="KILL"),
    ]
    out = render_summary(kpis)
    assert "Total: 3 campaigns analyzed" in out
    assert "SCALE: 1" in out
    assert "HOLD: 1" in out
    assert "KILL: 1" in out


def test_render_summary_uses_singular_campaign_for_single():
    """Single-campaign footer uses 'campaign' (singular), not 'campaigns'."""
    kpis = [CampaignKPI(campaign_key="FB", channel="FB", total_clicks=1,
                        total_commission_rm=1.0, epc=1.0, classification="SCALE")]
    out = render_summary(kpis)
    assert "1 campaign analyzed" in out  # no 's'
    assert "1 campaigns" not in out


# ---------------------------------------------------------------------------
# Integration test — real CSV fixtures end-to-end
# ---------------------------------------------------------------------------

def test_build_kpis_with_real_fixtures_end_to_end():
    """Load the three sample CSVs and verify the full join + classification.

    Sanity-checks against the brainstorming example:

    * Meta fixture: SIFAT ad-set has 6 ads (78+65+42+120+200 = 505 link clicks,
      10.36+9.75+8.40+12.00+16.00 = 56.51 spend) -> weighted CPC ~0.1119.
    * Shopee fixture: FB has 14 clicks (5 FB + 3+5+3 + ...), realized
      commission sum from Completed rows with sub_id1=FB.
    * With default map SIFAT -> FB, the FB KPI has both Meta + Shopee data.
    """
    meta_rows = parse_meta_csv(FIXTURES / "meta_sample.csv")
    click_rows = parse_click_csv(FIXTURES / "shopee_click_sample.csv")
    commission_rows = parse_commission_csv(FIXTURES / "shopee_commission_sample.csv")

    kpis = build_kpis(meta_rows, click_rows, commission_rows)

    # Three campaigns should appear: FB, produkFB, DIRECT.
    keys = sorted(k.campaign_key for k in kpis)
    assert keys == ["DIRECT", "FB", "produkFB"]

    by_key = {k.campaign_key: k for k in kpis}
    fb = by_key["FB"]
    produkfb = by_key["produkFB"]
    direct = by_key["DIRECT"]

    # FB has Meta spend (from SIFAT ad-set, joined via default map).
    assert fb.meta_spend_rm is not None
    assert fb.meta_link_clicks is not None
    assert fb.meta_link_clicks > 0

    # FB EPC must classify into one of the three tiers.
    assert fb.classification in CLASSIFICATIONS

    # produkFB joins with PRODUK ad-set (3 ads: VIDEO MEJA, LAMPU, BANTAL).
    assert produkfb.meta_spend_rm is not None
    assert produkfb.meta_link_clicks == 45  # 30 + 15 + 0 from fixture

    # DIRECT never has Meta spend — all Meta fields must be None.
    assert direct.meta_spend_rm is None
    assert direct.meta_link_clicks is None

    # The Pending commission row must NOT contribute to FB commission sum.
    # Realized FB commissions: 2.78 + 1.94 + 6.75 + 3.30 + 7.49 + 4.28 = 26.54
    assert fb.total_commission_rm == pytest.approx(26.54, abs=0.01)


def test_render_summary_with_real_fixtures_is_non_empty_well_formed():
    """Smoke test: real data -> report with all three tiers and per-campaign blocks."""
    meta_rows = parse_meta_csv(FIXTURES / "meta_sample.csv")
    click_rows = parse_click_csv(FIXTURES / "shopee_click_sample.csv")
    commission_rows = parse_commission_csv(FIXTURES / "shopee_commission_sample.csv")

    kpis = build_kpis(meta_rows, click_rows, commission_rows)
    out = render_summary(kpis)

    # Title + all three tier headers + footer + at least one campaign block.
    assert "Hermes Traffic Intelligence Report" in out
    assert "SCALE" in out
    assert "HOLD" in out
    assert "KILL" in out
    assert "Total:" in out
    # All three real campaign keys must appear.
    for key in ("FB", "produkFB", "DIRECT"):
        assert key in out
