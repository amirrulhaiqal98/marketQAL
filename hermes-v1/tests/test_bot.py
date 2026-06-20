"""
Tests for :mod:`bot`.

Covers the pure helpers and :class:`bot.SessionState` — the parts that
are testable without spinning up python-telegram-bot. The actual async
``update`` / ``context`` handlers are exercised manually in Phase 1
(``python bot.py`` smoke test) and don't need unit coverage per the
brainstorming scope guardrails.

What this file covers:

* :func:`bot.split_score_args`            — argument parsing (8 cases)
* :func:`bot.detect_shopee_csv_kind`      — header sniffing (4 cases)
* :func:`bot.format_score_message`        — Telegram text rendering (3)
* :func:`bot.format_meta_summary`         — Meta upload ack (4)
* :func:`bot.format_uploads_required`     — /insights error message (3)
* :func:`bot.truncate_for_telegram`       — message-length guard (4)
* :func:`bot.sanitize_filename`           — path-traversal defense (5)
* :func:`bot.build_save_path`             — uploads-dir layout (3)
* :class:`bot.SessionState`               — in-memory state machine (12)
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from bot import (
    SessionState,
    build_save_path,
    detect_shopee_csv_kind,
    format_meta_summary,
    format_score_message,
    format_uploads_required,
    sanitize_filename,
    split_score_args,
    truncate_for_telegram,
)
from modules.models import (
    MetaAdRow,
    ScoringResult,
    ShopeeClickRow,
    ShopeeCommissionRow,
)


# ---------------------------------------------------------------------------
# Tiny factories for the row types — keep tests readable.
# ---------------------------------------------------------------------------

def _meta_row(
    *,
    spend: float = 50.0,
    clicks: int = 100,
    ad_set: str = "US-earbuds-v1",
    tarikh_mula: date | None = None,
    tarikh_tamat: date | None = None,
) -> MetaAdRow:
    return MetaAdRow(
        nama_iklan=f"{ad_set}-creative",
        nama_set_iklan=ad_set,
        jenis_bajet="Using campaign budget",
        tarikh_mula=tarikh_mula or date(2025, 12, 1),
        tarikh_tamat=tarikh_tamat or date(2025, 12, 7),
        hasil=clicks,
        result_indicator="actions:link_click",
        kos_bagi_setiap_hasil=spend / clicks if clicks else 0.0,
        jumlah_dibelanjakan=spend,
        teraan=clicks * 100,
        capaian=clicks * 80,
        link_clicks=clicks,
    )


def _click_row(sub_id: str = "abc123") -> ShopeeClickRow:
    return ShopeeClickRow(
        click_id=f"click-{sub_id}",
        click_time=datetime(2025, 12, 1, 10, 0, 0),
        click_region="Malaysia",
        sub_id_raw=sub_id,
        referrer="Facebook",
    )


def _commission_row(order_id: str = "ord-1", realized: bool = True) -> ShopeeCommissionRow:
    return ShopeeCommissionRow(
        order_id=order_id,
        order_status="Completed" if realized else "Pending",
        conversion_id=f"conv-{order_id}",
        order_time=datetime(2025, 12, 2, 11, 0, 0),
        complete_time=datetime(2025, 12, 3, 12, 0, 0) if realized else None,
        click_time=datetime(2025, 12, 1, 10, 0, 0),
        shop_name="TestShop",
        item_name="Test Item",
        price_rm=29.90,
        qty=1,
        purchase_value_rm=29.90,
        affiliate_net_commission_rm=2.99,
        sub_id1="abc123",
        channel="Facebook",
        is_realized=realized,
    )


def _scoring_result(
    *, score: int = 82, fb: list[str] | None = None, th: list[str] | None = None,
    reasoning: str = "Test reasoning line.",
) -> ScoringResult:
    return ScoringResult(
        score=score,
        fb_hooks=fb if fb is not None else [
            "FB hook one",
            "FB hook two",
            "FB hook three",
        ],
        threads_hooks=th if th is not None else [
            "Threads hook one",
            "Threads hook two",
            "Threads hook three",
        ],
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# split_score_args — 8 cases
# ---------------------------------------------------------------------------

class TestSplitScoreArgs:
    def test_valid_inputs_returns_typed_tuple(self) -> None:
        title, price, category = split_score_args(
            "Wireless Earbuds | 29.90 | Electronics"
        )
        assert title == "Wireless Earbuds"
        assert price == 29.90
        assert category == "Electronics"

    def test_strips_whitespace_around_each_part(self) -> None:
        title, price, category = split_score_args(
            "  Spaced Item   |   100.00   |   Toys  "
        )
        assert title == "Spaced Item"
        assert price == 100.00
        assert category == "Toys"

    def test_integer_price_accepted(self) -> None:
        _, price, _ = split_score_args("X | 10 | Y")
        assert price == 10.0
        assert isinstance(price, float)

    def test_zero_price_accepted(self) -> None:
        _, price, _ = split_score_args("Free sample | 0 | Promo")
        assert price == 0.0

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(ValueError, match="Price must be >= 0"):
            split_score_args("X | -5 | Y")

    def test_non_numeric_price_rejected(self) -> None:
        with pytest.raises(ValueError, match="Price must be a number"):
            split_score_args("X | twelve | Y")

    def test_wrong_number_of_parts_rejected(self) -> None:
        with pytest.raises(ValueError, match="Expected 3 parts"):
            split_score_args("Only one part")
        with pytest.raises(ValueError, match="Expected 3 parts"):
            split_score_args("A | B")

    def test_empty_title_rejected(self) -> None:
        with pytest.raises(ValueError, match="Title must be non-empty"):
            split_score_args(" | 10 | Toys")

    def test_empty_category_rejected(self) -> None:
        with pytest.raises(ValueError, match="Category must be non-empty"):
            split_score_args("Title | 10 |   ")


# ---------------------------------------------------------------------------
# detect_shopee_csv_kind — 4 cases
# ---------------------------------------------------------------------------

class TestDetectShopeeCsvKind:
    def test_click_csv_detected_by_sub_id(self, tmp_path: Path) -> None:
        p = tmp_path / "clicks.csv"
        p.write_text(
            "Sub_id,Click id,Click Time\nabc,1,2025-12-01\n",
            encoding="utf-8",
        )
        assert detect_shopee_csv_kind(p) == "click"

    def test_click_csv_detected_by_click_id(self, tmp_path: Path) -> None:
        p = tmp_path / "clicks.csv"
        p.write_text(
            "Click id,Click Time\nc-1,2025-12-01\n",
            encoding="utf-8",
        )
        assert detect_shopee_csv_kind(p) == "click"

    def test_commission_csv_detected_by_order_id(self, tmp_path: Path) -> None:
        p = tmp_path / "comm.csv"
        p.write_text(
            "Order id,Order Status,Commission\n1,completed,1.50\n",
            encoding="utf-8",
        )
        assert detect_shopee_csv_kind(p) == "commission"

    def test_commission_csv_detected_by_order_status(self, tmp_path: Path) -> None:
        p = tmp_path / "comm.csv"
        p.write_text(
            "Order Status,Commission\ncompleted,1.50\n",
            encoding="utf-8",
        )
        assert detect_shopee_csv_kind(p) == "commission"

    def test_unknown_header_raises_value_error(self, tmp_path: Path) -> None:
        p = tmp_path / "mystery.csv"
        p.write_text("foo,bar,baz\n1,2,3\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Unknown Shopee CSV"):
            detect_shopee_csv_kind(p)

    def test_missing_file_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Cannot read CSV header"):
            detect_shopee_csv_kind(tmp_path / "does-not-exist.csv")

    def test_bom_in_header_is_handled(self, tmp_path: Path) -> None:
        # utf-8-sig decoding strips BOM if present.
        p = tmp_path / "with_bom.csv"
        p.write_bytes(b"\xef\xbb\xbfSub_id,Click id\nabc,1\n")
        assert detect_shopee_csv_kind(p) == "click"

    def test_header_matching_is_case_insensitive(self, tmp_path: Path) -> None:
        p = tmp_path / "mixed_case.csv"
        p.write_text("ORDER ID,ORDER STATUS\n", encoding="utf-8")
        assert detect_shopee_csv_kind(p) == "commission"


# ---------------------------------------------------------------------------
# format_score_message — 3 cases
# ---------------------------------------------------------------------------

class TestFormatScoreMessage:
    def test_includes_score_and_metadata(self) -> None:
        result = _scoring_result(score=78)
        text = format_score_message(
            result, title="Wireless Earbuds", price=29.90, category="Electronics",
        )
        assert "78/100" in text
        assert "Wireless Earbuds" in text
        assert "RM 29.90" in text
        assert "Electronics" in text

    def test_numbers_all_facebook_hooks(self) -> None:
        result = _scoring_result(
            fb=["hook A", "hook B", "hook C"],
            th=["t1"],
        )
        text = format_score_message(
            result, title="X", price=1.0, category="Y",
        )
        assert "1. hook A" in text
        assert "2. hook B" in text
        assert "3. hook C" in text
        assert "1. t1" in text

    def test_includes_reasoning_line(self) -> None:
        result = _scoring_result(reasoning="Hook sells scarcity.")
        text = format_score_message(
            result, title="X", price=1.0, category="Y",
        )
        assert "Why: Hook sells scarcity." in text


# ---------------------------------------------------------------------------
# format_meta_summary — 4 cases
# ---------------------------------------------------------------------------

class TestFormatMetaSummary:
    def test_empty_rows_message(self) -> None:
        assert format_meta_summary([]) == (
            "✅ Meta CSV loaded but contained 0 parseable rows."
        )

    def test_single_row_summary(self) -> None:
        row = _meta_row(spend=42.5, clicks=21, ad_set="set-A")
        text = format_meta_summary([row])
        assert "1 rows" in text
        assert "RM 42.50" in text
        assert "21" in text  # clicks
        assert "set-A" in text
        assert "2025-12-01 → 2025-12-07" in text

    def test_multiple_rows_aggregates(self) -> None:
        rows = [
            _meta_row(spend=10.0, clicks=5, ad_set="B"),
            _meta_row(spend=20.0, clicks=15, ad_set="A"),
            _meta_row(spend=30.0, clicks=30, ad_set="A"),
        ]
        text = format_meta_summary(rows)
        assert "3 rows" in text
        assert "RM 60.00" in text  # 10+20+30
        assert "50" in text       # 5+15+30
        # ad-sets sorted alphabetically
        assert "A, B" in text

    def test_period_extremes(self) -> None:
        rows = [
            _meta_row(
                tarikh_mula=date(2025, 1, 1),
                tarikh_tamat=date(2025, 1, 7),
            ),
            _meta_row(
                tarikh_mula=date(2025, 3, 1),
                tarikh_tamat=date(2025, 3, 7),
            ),
        ]
        text = format_meta_summary(rows)
        assert "2025-01-01 → 2025-03-07" in text


# ---------------------------------------------------------------------------
# format_uploads_required — 3 cases
# ---------------------------------------------------------------------------

class TestFormatUploadsRequired:
    def test_all_loaded_message(self) -> None:
        state = SessionState(
            meta_rows=[_meta_row()],
            click_rows=[_click_row()],
            commission_rows=[_commission_row()],
        )
        assert format_uploads_required(state) == "All datasets loaded."

    def test_missing_meta(self) -> None:
        state = SessionState(
            click_rows=[_click_row()],
            commission_rows=[_commission_row()],
        )
        text = format_uploads_required(state)
        assert "/insights" in text
        assert "Meta CSV" in text
        assert "Shopee click CSV" not in text
        assert "Shopee commission CSV" not in text

    def test_all_missing(self) -> None:
        text = format_uploads_required(SessionState())
        assert "/analyze_meta" in text
        assert "/analyze_shopee" in text
        assert text.count("•") == 3


# ---------------------------------------------------------------------------
# truncate_for_telegram — 4 cases
# ---------------------------------------------------------------------------

class TestTruncateForTelegram:
    def test_short_text_unchanged(self) -> None:
        assert truncate_for_telegram("hello", limit=100) == "hello"

    def test_exact_length_unchanged(self) -> None:
        text = "x" * 50
        assert truncate_for_telegram(text, limit=50) == text

    def test_long_text_truncated_with_marker(self) -> None:
        text = "A" * 1000
        out = truncate_for_telegram(text, limit=100)
        assert len(out) <= 100
        assert "[...truncated" in out
        assert out.startswith("A")

    def test_marker_overflow_falls_back_to_hard_truncate(self) -> None:
        # Use a tiny limit so the marker itself doesn't fit.
        text = "A" * 200
        out = truncate_for_telegram(text, limit=10)
        # Marker can't fit; we hard-truncate to the limit.
        assert out == "A" * 10


# ---------------------------------------------------------------------------
# sanitize_filename — 5 cases
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    def test_normal_name_kept(self) -> None:
        assert sanitize_filename("export.csv") == "export.csv"

    def test_path_components_stripped(self) -> None:
        # Path traversal — only the basename should survive.
        assert sanitize_filename("../../etc/passwd") == "passwd"

    def test_absolute_path_stripped_to_basename(self) -> None:
        assert sanitize_filename("/var/data/file.csv") == "file.csv"

    def test_empty_string_falls_back_to_upload(self) -> None:
        assert sanitize_filename("") == "upload"

    def test_dot_only_name_falls_back_to_upload(self) -> None:
        assert sanitize_filename("...") == "upload"
        assert sanitize_filename(".   ") == "upload"


# ---------------------------------------------------------------------------
# build_save_path — 3 cases
# ---------------------------------------------------------------------------

class TestBuildSavePath:
    def test_layout_includes_chat_id_and_basename(self, tmp_path: Path) -> None:
        p = build_save_path(tmp_path, 12345, "data.csv")
        assert p.parent == tmp_path
        assert p.name.endswith("_data.csv")
        assert "12345_" in p.name

    def test_strips_traversal_in_original_name(self, tmp_path: Path) -> None:
        p = build_save_path(tmp_path, 1, "../../evil.csv")
        # No parent-directory markers in the final filename component.
        assert ".." not in p.name
        assert p.name.endswith("_evil.csv")

    def test_two_calls_in_different_seconds_have_different_paths(
        self, tmp_path: Path,
    ) -> None:
        import time

        p1 = build_save_path(tmp_path, 1, "x.csv")
        time.sleep(1.05)
        p2 = build_save_path(tmp_path, 1, "x.csv")
        assert p1 != p2


# ---------------------------------------------------------------------------
# SessionState — 12 cases
# ---------------------------------------------------------------------------

class TestSessionState:
    def test_default_state_is_empty(self) -> None:
        s = SessionState()
        assert s.meta_rows == []
        assert s.click_rows == []
        assert s.commission_rows == []
        assert s.has_meta() is False
        assert s.has_clicks() is False
        assert s.has_commissions() is False
        assert s.has_full_dataset() is False
        assert s.has_shopee_partial() is False
        assert s.missing_for_insights() == [
            "Meta CSV — /analyze_meta",
            "Shopee click CSV — /analyze_shopee",
            "Shopee commission CSV — /analyze_shopee",
        ]

    def test_set_meta_replaces_existing(self) -> None:
        s = SessionState()
        s.set_meta([_meta_row(ad_set="A"), _meta_row(ad_set="B")])
        assert len(s.meta_rows) == 2
        assert {r.nama_set_iklan for r in s.meta_rows} == {"A", "B"}
        # replace, not append
        s.set_meta([_meta_row(ad_set="C")])
        assert len(s.meta_rows) == 1
        assert s.meta_rows[0].nama_set_iklan == "C"

    def test_set_clicks_replaces_existing(self) -> None:
        s = SessionState()
        s.set_clicks([_click_row("x")])
        assert len(s.click_rows) == 1
        s.set_clicks([_click_row("y"), _click_row("z")])
        assert len(s.click_rows) == 2

    def test_set_commissions_replaces_existing(self) -> None:
        s = SessionState()
        s.set_commissions([_commission_row("o1")])
        s.set_commissions([_commission_row("o2"), _commission_row("o3")])
        assert len(s.commission_rows) == 2

    def test_set_methods_copy_input_list(self) -> None:
        # Mutating the caller's list after .set_* shouldn't change state.
        s = SessionState()
        rows = [_meta_row(ad_set="A")]
        s.set_meta(rows)
        rows.append(_meta_row(ad_set="B"))
        assert len(s.meta_rows) == 1

    def test_has_meta_true_when_rows_present(self) -> None:
        s = SessionState(meta_rows=[_meta_row()])
        assert s.has_meta() is True
        assert s.has_full_dataset() is False

    def test_has_clicks_true_when_rows_present(self) -> None:
        s = SessionState(click_rows=[_click_row()])
        assert s.has_clicks() is True
        assert s.has_shopee_partial() is True
        assert s.has_full_dataset() is False

    def test_has_commissions_true_when_rows_present(self) -> None:
        s = SessionState(commission_rows=[_commission_row()])
        assert s.has_commissions() is True
        assert s.has_shopee_partial() is True

    def test_has_full_dataset_requires_all_three(self) -> None:
        s = SessionState(
            meta_rows=[_meta_row()],
            click_rows=[_click_row()],
        )
        assert s.has_full_dataset() is False
        s.set_commissions([_commission_row()])
        assert s.has_full_dataset() is True

    def test_clear_empties_everything(self) -> None:
        s = SessionState(
            meta_rows=[_meta_row()],
            click_rows=[_click_row()],
            commission_rows=[_commission_row()],
        )
        s.clear()
        assert s.has_full_dataset() is False
        assert s.has_shopee_partial() is False
        assert s.meta_rows == []
        assert s.click_rows == []
        assert s.commission_rows == []

    def test_missing_for_insights_lists_only_missing(self) -> None:
        s = SessionState(meta_rows=[_meta_row()])
        missing = s.missing_for_insights()
        assert missing == [
            "Shopee click CSV — /analyze_shopee",
            "Shopee commission CSV — /analyze_shopee",
        ]

    def test_dataclass_equality(self) -> None:
        # Sanity: two freshly-created SessionStates are equal.
        assert SessionState() == SessionState()
        s = SessionState(meta_rows=[_meta_row()])
        assert s != SessionState()
