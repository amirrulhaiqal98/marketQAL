"""
Tests for ``modules.shopee_analyzer``.

The Shopee Affiliate exports drive the entire pipeline — without correct
``campaign_key`` derivation the EPC numbers that come out of
``insight_engine`` would silently be wrong. So this suite is explicit about:

* ``_derive_campaign_key`` — every permutation of trailing dashes and
  whitespace (defensive against Shopee-side schema drift).
* ``parse_click_csv`` / ``parse_commission_csv`` — header normalization,
  datetime parsing, type coercion, ``is_realized`` derivation.
* ``aggregate_by_campaign`` — exact totals and unique-conversion counting
  for the multi-item order (``260618D4CNWBBW`` → 3 items, 1 click).
* ``top_channel`` — tie-breaking is deterministic.

The fixtures used here are committed to ``data/fixtures/`` so the suite is
fully self-contained.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from modules import shopee_analyzer
from modules.constants import DIRECT_TRAFFIC_KEY
from modules.models import ShopeeClickRow, ShopeeCommissionRow


# ---------------------------------------------------------------------------
# Path helpers — point at fixtures regardless of cwd
# ---------------------------------------------------------------------------

FIXTURES_DIR: Path = Path(__file__).resolve().parent.parent / "data" / "fixtures"
CLICK_CSV: Path = FIXTURES_DIR / "shopee_click_sample.csv"
COMMISSION_CSV: Path = FIXTURES_DIR / "shopee_commission_sample.csv"


# ---------------------------------------------------------------------------
# _derive_campaign_key — pure-function tests
# ---------------------------------------------------------------------------

class TestDeriveCampaignKey:
    """The ``Sub_id`` / ``Sub_id1`` → ``campaign_key`` derivation rule."""

    def test_fb_with_four_trailing_dashes(self) -> None:
        assert shopee_analyzer._derive_campaign_key("FB----") == "FB"

    def test_produkfb_with_four_trailing_dashes(self) -> None:
        assert shopee_analyzer._derive_campaign_key("produkFB----") == "produkFB"

    def test_only_dashes_returns_direct(self) -> None:
        assert shopee_analyzer._derive_campaign_key("----") == DIRECT_TRAFFIC_KEY

    def test_empty_string_returns_direct(self) -> None:
        assert shopee_analyzer._derive_campaign_key("") == DIRECT_TRAFFIC_KEY

    def test_already_clean_fb(self) -> None:
        """Commission rows ship with the trailing dashes already stripped."""
        assert shopee_analyzer._derive_campaign_key("FB") == "FB"

    def test_already_clean_produkfb(self) -> None:
        assert shopee_analyzer._derive_campaign_key("produkFB") == "produkFB"

    def test_none_returns_direct(self) -> None:
        """Defensive: ``None`` Sub_id values (rarely seen in real exports)
        must not crash the pipeline."""
        assert shopee_analyzer._derive_campaign_key(None) == DIRECT_TRAFFIC_KEY

    def test_more_than_four_trailing_dashes_still_strips(self) -> None:
        """Some Shopee exports have stray extra dashes — rstrip handles all."""
        assert shopee_analyzer._derive_campaign_key("FB-----") == "FB"
        assert shopee_analyzer._derive_campaign_key("produkFB------") == "produkFB"

    def test_one_trailing_dash_strips(self) -> None:
        assert shopee_analyzer._derive_campaign_key("FB-") == "FB"

    def test_whitespace_is_trimmed(self) -> None:
        assert shopee_analyzer._derive_campaign_key("  FB----  ") == "FB"
        assert shopee_analyzer._derive_campaign_key("   ") == DIRECT_TRAFFIC_KEY

    def test_internal_dashes_are_preserved(self) -> None:
        """We only rstrip — internal dashes survive (none expected in real
        data, but we don't want a future Shopee schema change to silently
        mutate keys)."""
        assert shopee_analyzer._derive_campaign_key("foo-bar----") == "foo-bar"

    def test_returns_direct_constant(self) -> None:
        """The DIRECT sentinel must equal :data:`DIRECT_TRAFFIC_KEY` so the
        constant is the single source of truth."""
        assert shopee_analyzer._derive_campaign_key("----") == DIRECT_TRAFFIC_KEY


# ---------------------------------------------------------------------------
# parse_click_csv — integration tests against the committed fixture
# ---------------------------------------------------------------------------

class TestParseClickCsv:
    """25-row Shopee click fixture: 14 FB + 8 produkFB + 3 DIRECT."""

    def test_returns_list_of_dataclass_instances(self) -> None:
        rows = shopee_analyzer.parse_click_csv(CLICK_CSV)
        assert isinstance(rows, list)
        assert len(rows) == 25
        assert all(isinstance(r, ShopeeClickRow) for r in rows)

    def test_preserves_csv_row_order(self) -> None:
        rows = shopee_analyzer.parse_click_csv(CLICK_CSV)
        assert rows[0].click_id == "CL260618001"
        assert rows[-1].click_id == "CL260618025"

    def test_click_time_is_datetime(self) -> None:
        rows = shopee_analyzer.parse_click_csv(CLICK_CSV)
        assert all(isinstance(r.click_time, datetime) for r in rows)

    def test_first_row_datetime_value(self) -> None:
        rows = shopee_analyzer.parse_click_csv(CLICK_CSV)
        assert rows[0].click_time == datetime(2026, 6, 18, 9, 1, 23)

    def test_sub_id_raw_stripped(self) -> None:
        rows = shopee_analyzer.parse_click_csv(CLICK_CSV)
        # FB row — trailing dashes preserved on sub_id_raw (raw value).
        assert rows[0].sub_id_raw == "FB----"
        # DIRECT row — only dashes.
        direct_rows = [r for r in rows if r.campaign_key == DIRECT_TRAFFIC_KEY]
        assert all(r.sub_id_raw == "----" for r in direct_rows)

    def test_referrer_preserved(self) -> None:
        rows = shopee_analyzer.parse_click_csv(CLICK_CSV)
        referrers = {r.referrer for r in rows}
        assert referrers == {"Facebook", "Instagram", "WhatsApp", "Telegram", "Others"}

    def test_click_region_preserved(self) -> None:
        rows = shopee_analyzer.parse_click_csv(CLICK_CSV)
        regions = {r.click_region for r in rows}
        assert regions == {"Malaysia", "Indonesia", "Philippines", "Vietnam"}

    def test_click_id_preserved(self) -> None:
        rows = shopee_analyzer.parse_click_csv(CLICK_CSV)
        assert rows[0].click_id == "CL260618001"
        # IDs should all start with CL (sanity).
        assert all(r.click_id.startswith("CL260618") for r in rows)

    def test_campaign_key_distribution(self) -> None:
        """Exact split: 14 FB, 8 produkFB, 3 DIRECT — derived from the fixture
        by hand so any accidental row drift trips this test."""
        rows = shopee_analyzer.parse_click_csv(CLICK_CSV)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.campaign_key] = counts.get(r.campaign_key, 0) + 1
        assert counts == {"FB": 14, "produkFB": 8, DIRECT_TRAFFIC_KEY: 3}

    def test_missing_required_column_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.csv"
        bad.write_text("Click id,Click Time,Country\nCL1,2026-06-18 09:00:00,Malaysia\n")
        with pytest.raises(ValueError, match="Shopee click CSV missing required columns"):
            shopee_analyzer.parse_click_csv(bad)

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            shopee_analyzer.parse_click_csv(tmp_path / "nope.csv")

    def test_empty_data_rows_returns_empty_list(self, tmp_path: Path) -> None:
        """Header-only CSV is valid input (e.g. user uploaded a stale export)."""
        header_only = tmp_path / "header.csv"
        header_only.write_text(
            "Click id,Click Time,Country,Sub_id,Referrer\n"
        )
        assert shopee_analyzer.parse_click_csv(header_only) == []

    def test_header_normalization_tolerates_extra_whitespace(
        self, tmp_path: Path
    ) -> None:
        csv_text = (
            "  Click id  , Click Time ,Country,Sub_id,Referrer\n"
            "CL1,2026-06-18 09:00:00,Malaysia,FB----,Facebook\n"
        )
        f = tmp_path / "whitespace.csv"
        f.write_text(csv_text)
        rows = shopee_analyzer.parse_click_csv(f)
        assert len(rows) == 1
        assert rows[0].click_id == "CL1"
        assert rows[0].sub_id_raw == "FB----"


# ---------------------------------------------------------------------------
# parse_commission_csv — integration tests against the committed fixture
# ---------------------------------------------------------------------------

class TestParseCommissionCsv:
    """10-row Shopee commission fixture: 9 Completed + 1 Pending (excluded
    from totals), multi-item order 260618D4CNWBBW with 3 items."""

    def test_returns_list_of_dataclass_instances(self) -> None:
        rows = shopee_analyzer.parse_commission_csv(COMMISSION_CSV)
        assert len(rows) == 10
        assert all(isinstance(r, ShopeeCommissionRow) for r in rows)

    def test_nine_completed_one_pending(self) -> None:
        rows = shopee_analyzer.parse_commission_csv(COMMISSION_CSV)
        realized = [r for r in rows if r.is_realized]
        unrealized = [r for r in rows if not r.is_realized]
        assert len(realized) == 9
        assert len(unrealized) == 1
        assert unrealized[0].order_id == "ORD26061805"
        assert unrealized[0].order_status == "Pending"

    def test_pending_row_has_no_complete_time(self) -> None:
        rows = shopee_analyzer.parse_commission_csv(COMMISSION_CSV)
        pending = next(r for r in rows if r.order_id == "ORD26061805")
        assert pending.complete_time is None
        assert pending.order_time is not None  # Pending rows still have Order Time.

    def test_multi_item_order_groups_under_one_conversion(self) -> None:
        """Conversion id ``260618D4CNWBBW`` has 3 items in the fixture. They
        must share the conversion_id so :func:`aggregate_by_campaign` can
        count them as a single click attribution."""
        rows = shopee_analyzer.parse_commission_csv(COMMISSION_CSV)
        multi = [r for r in rows if r.conversion_id == "260618D4CNWBBW"]
        assert len(multi) == 3
        order_ids = {r.order_id for r in multi}
        assert order_ids == {"ORD26061801", "ORD26061802", "ORD26061803"}

    def test_multi_item_order_shares_click_time(self) -> None:
        rows = shopee_analyzer.parse_commission_csv(COMMISSION_CSV)
        multi = [r for r in rows if r.conversion_id == "260618D4CNWBBW"]
        click_times = {r.click_time for r in multi}
        assert click_times == {datetime(2026, 6, 18, 9, 1, 23)}

    def test_numeric_fields_coerced(self) -> None:
        rows = shopee_analyzer.parse_commission_csv(COMMISSION_CSV)
        first = rows[0]
        assert isinstance(first.price_rm, float)
        assert isinstance(first.qty, int)
        assert isinstance(first.purchase_value_rm, float)
        assert isinstance(first.affiliate_net_commission_rm, float)
        # Sanity-check the first row's exact numbers.
        assert first.price_rm == 18.50
        assert first.qty == 1
        assert first.purchase_value_rm == 18.50
        assert first.affiliate_net_commission_rm == 2.78

    def test_campaign_key_distribution(self) -> None:
        """6 FB (5 distinct + 1 multi-item extra) + 3 produkFB + 1 DIRECT
        (the Pending row, whose Sub_id1 is empty)."""
        rows = shopee_analyzer.parse_commission_csv(COMMISSION_CSV)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.campaign_key] = counts.get(r.campaign_key, 0) + 1
        assert counts == {"FB": 6, "produkFB": 3, DIRECT_TRAFFIC_KEY: 1}

    def test_sub_id1_stripped_at_parse_time(self) -> None:
        """Even if Shopee ever ships trailing dashes on Sub_id1 (real
        Sub_id values do; Sub_id1 may too), the campaign_key derivation
        handles it."""
        rows = shopee_analyzer.parse_commission_csv(COMMISSION_CSV)
        # Commission rows in the fixture ship clean: sub_id1 == campaign_key.
        for r in rows:
            if r.sub_id1:
                assert r.campaign_key == r.sub_id1

    def test_order_id_preserved(self) -> None:
        rows = shopee_analyzer.parse_commission_csv(COMMISSION_CSV)
        assert rows[0].order_id == "ORD26061801"
        assert rows[-1].order_id == "ORD26061810"

    def test_channel_preserved(self) -> None:
        rows = shopee_analyzer.parse_commission_csv(COMMISSION_CSV)
        channels = {r.channel for r in rows}
        assert channels == {"Facebook", "Instagram", "Websites"}

    def test_missing_required_column_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.csv"
        bad.write_text("Order id,Order Status\nORD1,Completed\n")
        with pytest.raises(
            ValueError, match="Shopee commission CSV missing required columns"
        ):
            shopee_analyzer.parse_commission_csv(bad)

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            shopee_analyzer.parse_commission_csv(tmp_path / "nope.csv")

    def test_tolerates_space_in_price_header(self, tmp_path: Path) -> None:
        """Some Shopee exports write ``Price (RM)`` with a space; both forms
        must work."""
        text = (
            "Order id,Order Status,Conversion id,Order Time,Complete Time,"
            "Click Time,Shop Name,Item Name,Price (RM),Qty,Purchase Value (RM),"
            "Affiliate Net Commission (RM),Sub_id1,Channel\n"
            "ORD1,Completed,CONV1,2026-06-18 10:00:00,2026-06-18 12:00:00,"
            "2026-06-18 09:00:00,ShopA,ItemA,10.00,1,10.00,1.50,FB,Facebook\n"
        )
        f = tmp_path / "spaced.csv"
        f.write_text(text)
        rows = shopee_analyzer.parse_commission_csv(f)
        assert len(rows) == 1
        assert rows[0].price_rm == 10.00
        assert rows[0].purchase_value_rm == 10.00
        assert rows[0].affiliate_net_commission_rm == 1.50

    def test_tolerates_empty_order_time(self, tmp_path: Path) -> None:
        """Some rows may have a blank Order Time (e.g. only Complete Time)."""
        text = (
            "Order id,Order Status,Conversion id,Order Time,Complete Time,"
            "Click Time,Shop Name,Item Name,Price(RM),Qty,Purchase Value(RM),"
            "Affiliate Net Commission(RM),Sub_id1,Channel\n"
            "ORD1,Completed,CONV1,,2026-06-18 12:00:00,"
            "2026-06-18 09:00:00,ShopA,ItemA,10.00,1,10.00,1.50,FB,Facebook\n"
        )
        f = tmp_path / "blank_order_time.csv"
        f.write_text(text)
        rows = shopee_analyzer.parse_commission_csv(f)
        assert rows[0].order_time is None
        assert rows[0].complete_time is not None


# ---------------------------------------------------------------------------
# aggregate_by_campaign — exact totals against the committed fixtures
# ---------------------------------------------------------------------------

class TestAggregateByCampaign:
    """End-to-end math: clicks + commissions → per-campaign KPI buckets."""

    @pytest.fixture()
    def clicks(self) -> list[ShopeeClickRow]:
        return shopee_analyzer.parse_click_csv(CLICK_CSV)

    @pytest.fixture()
    def commissions(self) -> list[ShopeeCommissionRow]:
        return shopee_analyzer.parse_commission_csv(COMMISSION_CSV)

    def test_returns_three_campaigns(self, clicks, commissions) -> None:
        result = shopee_analyzer.aggregate_by_campaign(clicks, commissions)
        assert set(result.keys()) == {"FB", "produkFB", DIRECT_TRAFFIC_KEY}

    def test_fb_total_clicks(self, clicks, commissions) -> None:
        result = shopee_analyzer.aggregate_by_campaign(clicks, commissions)
        assert result["FB"]["total_clicks"] == 14

    def test_fb_total_commission(self, clicks, commissions) -> None:
        """2.78 + 1.94 + 6.75 + 3.30 + 7.49 + 4.28 = 26.54 RM
        (includes the multi-item order 260618D4CNWBBW summed naturally)."""
        result = shopee_analyzer.aggregate_by_campaign(clicks, commissions)
        assert result["FB"]["total_commission_rm"] == pytest.approx(26.54)

    def test_fb_epc(self, clicks, commissions) -> None:
        result = shopee_analyzer.aggregate_by_campaign(clicks, commissions)
        assert result["FB"]["epc"] == pytest.approx(26.54 / 14)

    def test_fb_unique_conversions(self, clicks, commissions) -> None:
        """4 unique Conversion ids: 260618D4CNWBBW (×3 items), 260618D4CNWBBR,
        260618D4CNWBBT, 260618D4CNWBBV."""
        result = shopee_analyzer.aggregate_by_campaign(clicks, commissions)
        assert result["FB"]["total_orders"] == 4

    def test_fb_completed_commission_rows(self, clicks, commissions) -> None:
        """6 raw commission rows for FB (multi-item order contributes 3)."""
        result = shopee_analyzer.aggregate_by_campaign(clicks, commissions)
        assert result["FB"]["completed_commission_rows"] == 6

    def test_produkfb_totals(self, clicks, commissions) -> None:
        """8 clicks, 3 unique conversions, 3 commission rows.
        Commission: 4.49 + 5.25 + 2.39 = 12.13 RM."""
        result = shopee_analyzer.aggregate_by_campaign(clicks, commissions)
        assert result["produkFB"]["total_clicks"] == 8
        assert result["produkFB"]["total_commission_rm"] == pytest.approx(12.13)
        assert result["produkFB"]["epc"] == pytest.approx(12.13 / 8)
        assert result["produkFB"]["total_orders"] == 3
        assert result["produkFB"]["completed_commission_rows"] == 3

    def test_direct_totals_exclude_pending(self, clicks, commissions) -> None:
        """DIRECT has 3 clicks but the only commission row (ORD05) is
        Pending — so commission total and EPC must be 0."""
        result = shopee_analyzer.aggregate_by_campaign(clicks, commissions)
        assert result[DIRECT_TRAFFIC_KEY]["total_clicks"] == 3
        assert result[DIRECT_TRAFFIC_KEY]["total_commission_rm"] == 0.0
        assert result[DIRECT_TRAFFIC_KEY]["epc"] == 0.0
        assert result[DIRECT_TRAFFIC_KEY]["completed_commission_rows"] == 0

    def test_channel_breakdown_fb(self, clicks, commissions) -> None:
        """14 FB clicks: 9 Facebook + 4 Instagram + 1 WhatsApp.
        The aggregator only records channels it actually sees, so the dict
        contains exactly those three keys (not zero-valued sentinels)."""
        result = shopee_analyzer.aggregate_by_campaign(clicks, commissions)
        assert result["FB"]["channels"] == {
            "Facebook": 9,
            "Instagram": 4,
            "WhatsApp": 1,
        }
        # Total over channels must equal total_clicks for FB.
        assert sum(result["FB"]["channels"].values()) == 14

    def test_region_breakdown(self, clicks, commissions) -> None:
        """Malaysia dominates (20 of 25 clicks)."""
        result = shopee_analyzer.aggregate_by_campaign(clicks, commissions)
        # Direct contains CL006, CL012, CL023 — all Malaysia → region count 3.
        assert result[DIRECT_TRAFFIC_KEY]["regions"]["Malaysia"] == 3
        # Total Malaysia across all campaigns must be 20.
        malaysia_total = sum(
            result[ck]["regions"].get("Malaysia", 0) for ck in result
        )
        assert malaysia_total == 20

    def test_empty_inputs_return_empty_dict(self) -> None:
        result = shopee_analyzer.aggregate_by_campaign([], [])
        assert result == {}

    def test_clicks_only_no_commissions(self) -> None:
        """We still want per-campaign click counts even before commissions
        are uploaded (real Telegram flow: user uploads clicks first, then
        commissions)."""
        clicks = shopee_analyzer.parse_click_csv(CLICK_CSV)
        result = shopee_analyzer.aggregate_by_campaign(clicks, [])
        assert result["FB"]["total_clicks"] == 14
        assert result["FB"]["total_commission_rm"] == 0.0
        assert result["FB"]["epc"] == 0.0  # division-by-zero guard


# ---------------------------------------------------------------------------
# top_channel — single-winner + tie-break + empty-input tests
# ---------------------------------------------------------------------------

class TestTopChannel:
    def test_returns_facebook_for_default_fixture(self) -> None:
        clicks = shopee_analyzer.parse_click_csv(CLICK_CSV)
        referrer, count = shopee_analyzer.top_channel(clicks)
        assert referrer == "Facebook"
        assert count == 17

    def test_empty_input_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one ShopeeClickRow"):
            shopee_analyzer.top_channel([])

    def test_tie_broken_by_first_encountered(self) -> None:
        """When two referrers have equal counts, the one that appears first
        in the input wins (preserves CSV order)."""
        rows = [
            _mk_click(referrer="A"),
            _mk_click(referrer="B"),
            _mk_click(referrer="A"),
            _mk_click(referrer="B"),
        ]
        assert shopee_analyzer.top_channel(rows) == ("A", 2)

    def test_single_channel(self) -> None:
        rows = [_mk_click(referrer="Facebook") for _ in range(5)]
        assert shopee_analyzer.top_channel(rows) == ("Facebook", 5)


# ---------------------------------------------------------------------------
# Header tolerance — covers shopee_analyzer internal mapping directly
# ---------------------------------------------------------------------------

class TestHeaderNormalization:
    """Defensive tests for the internal ``_CLICK_HEADER_MAP`` and
    ``_COMMISSION_HEADER_MAP``. These should never break unless the public
    contract changes — but if they do, we want loud failures."""

    def test_click_header_map_keys_are_lowercase(self) -> None:
        for key in shopee_analyzer._CLICK_HEADER_MAP:
            assert key == key.lower()

    def test_commission_header_map_keys_are_lowercase(self) -> None:
        for key in shopee_analyzer._COMMISSION_HEADER_MAP:
            assert key == key.lower()

    def test_click_header_map_contains_required_fields(self) -> None:
        required = {
            "click_id",
            "click_time",
            "click_region",
            "sub_id_raw",
            "referrer",
        }
        assert required.issubset(set(shopee_analyzer._CLICK_HEADER_MAP.values()))

    def test_commission_header_map_contains_required_fields(self) -> None:
        required = {
            "order_id",
            "order_status",
            "conversion_id",
            "click_time",
            "shop_name",
            "item_name",
            "price_rm",
            "qty",
            "purchase_value_rm",
            "affiliate_net_commission_rm",
            "sub_id1",
            "channel",
        }
        assert required.issubset(set(shopee_analyzer._COMMISSION_HEADER_MAP.values()))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_click(referrer: str) -> ShopeeClickRow:
    """Build a minimal :class:`ShopeeClickRow` for unit tests."""
    return ShopeeClickRow(
        click_id="CL_TEST",
        click_time=datetime(2026, 6, 18, 9, 0, 0),
        click_region="Malaysia",
        sub_id_raw="FB----",
        referrer=referrer,
        campaign_key="FB",
    )
