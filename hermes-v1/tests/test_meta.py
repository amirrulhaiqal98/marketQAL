"""
Integration tests for ``modules.meta_analyzer``.

These tests load the real-format fixture CSV (``data/fixtures/meta_sample.csv``)
and validate:

* Header normalization (Bahasa Malaysia -> snake_case fields)
* Date parsing
* Numeric coercion (comma/RM-stripping defensiveness)
* Derived KPI computation (``link_clicks``, ``ctr``, ``cpc``)
* Ad-set aggregation (``summarize_campaign``)
* Best/worst creative selection
* Defensive paths (empty CSV, missing columns, no-impression rows)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from modules.meta_analyzer import (
    best_creative,
    parse_meta_csv,
    summarize_campaign,
    worst_creative,
)
from modules.models import MetaAdRow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "data" / "fixtures"
META_FIXTURE = FIXTURES_DIR / "meta_sample.csv"


@pytest.fixture(scope="module")
def meta_rows() -> list[MetaAdRow]:
    """Parse the canonical Meta fixture once per test module."""
    assert META_FIXTURE.exists(), f"Missing fixture: {META_FIXTURE}"
    return parse_meta_csv(META_FIXTURE)


# ---------------------------------------------------------------------------
# parse_meta_csv — header mapping + row count
# ---------------------------------------------------------------------------

def test_parse_meta_csv_returns_list_of_metarow(meta_rows):
    assert isinstance(meta_rows, list)
    assert len(meta_rows) == 8
    assert all(isinstance(r, MetaAdRow) for r in meta_rows)


def test_parse_meta_csv_preserves_csv_order(meta_rows):
    """Order in the output must match order in the CSV."""
    names = [r.nama_iklan for r in meta_rows]
    assert names == [
        "VIDEO PURDAH",
        "VIDEO GLOVE",
        "VIDEO BANGKU BOX",
        "VIDEO KERUSI",
        "VIDEO MEJA",
        "VIDEO LAMPU",
        "VIDEO BANTAL",
        "VIDEO TUDUNG SAJI",
    ]


def test_parse_meta_csv_maps_malay_headers_to_snake_case(meta_rows):
    """Spot-check that each Bahasa Malaysia header mapped correctly."""
    purdah = meta_rows[0]
    assert purdah.nama_iklan == "VIDEO PURDAH"
    assert purdah.nama_set_iklan == "SIFAT"
    assert purdah.jenis_bajet == "Using campaign budget"
    assert purdah.result_indicator == "actions:link_click"


def test_parse_meta_csv_strips_surrounding_whitespace(meta_rows):
    """The fixtures strip cleanly — no leading/trailing whitespace leaks."""
    for row in meta_rows:
        assert row.nama_iklan == row.nama_iklan.strip()
        assert row.nama_set_iklan == row.nama_set_iklan.strip()
        assert row.jenis_bajet == row.jenis_bajet.strip()


# ---------------------------------------------------------------------------
# parse_meta_csv — date parsing
# ---------------------------------------------------------------------------

def test_parse_meta_csv_parses_dates_as_date_objects(meta_rows):
    """Both date columns must parse into ``datetime.date`` (not strings)."""
    purdah = meta_rows[0]
    assert purdah.tarikh_mula == date(2026, 6, 14)
    assert purdah.tarikh_tamat == date(2026, 6, 18)
    assert isinstance(purdah.tarikh_mula, date)
    assert isinstance(purdah.tarikh_tamat, date)


def test_parse_meta_csv_date_range_is_widest_for_purdah(meta_rows):
    """Spot-check date spans across rows."""
    meja = meta_rows[4]
    assert meja.tarikh_mula == date(2026, 6, 15)
    assert meja.tarikh_tamat == date(2026, 6, 19)


# ---------------------------------------------------------------------------
# parse_meta_csv — numeric coercion
# ---------------------------------------------------------------------------

def test_parse_meta_csv_parses_integers_correctly(meta_rows):
    """``hasil``, ``teraan``, ``capaian`` are int columns."""
    purdah = meta_rows[0]
    assert purdah.hasil == 78
    assert purdah.teraan == 6403
    assert purdah.capaian == 6014
    assert isinstance(purdah.hasil, int)
    assert isinstance(purdah.teraan, int)
    assert isinstance(purdah.capaian, int)


def test_parse_meta_csv_parses_floats_correctly(meta_rows):
    """``kos_bagi_setiap_hasil`` and ``jumlah_dibelanjakan`` are floats."""
    purdah = meta_rows[0]
    assert purdah.kos_bagi_setiap_hasil == pytest.approx(0.13282051, abs=1e-9)
    assert purdah.jumlah_dibelanjakan == pytest.approx(10.36, abs=1e-9)
    assert isinstance(purdah.jumlah_dibelanjakan, float)


def test_parse_meta_csv_handles_zero_clicks_row(meta_rows):
    """VIDEO BANTAL has 0 link clicks — must parse cleanly."""
    bantal = meta_rows[6]
    assert bantal.nama_iklan == "VIDEO BANTAL"
    assert bantal.hasil == 0
    assert bantal.jumlah_dibelanjakan == 5.00


# ---------------------------------------------------------------------------
# parse_meta_csv — derived KPIs
# ---------------------------------------------------------------------------

def test_parse_meta_csv_derives_link_clicks_from_hasil(meta_rows):
    """For actions:link_click rows, ``link_clicks == hasil``."""
    for row in meta_rows:
        assert row.link_clicks == row.hasil


def test_parse_meta_csv_derives_ctr(meta_rows):
    """CTR = link_clicks / teraan, verified per row."""
    purdah = meta_rows[0]
    assert purdah.ctr == pytest.approx(78 / 6403, abs=1e-9)
    kerusi = meta_rows[3]
    assert kerusi.ctr == pytest.approx(120 / 4500, abs=1e-9)


def test_parse_meta_csv_derives_cpc(meta_rows):
    """CPC = jumlah_dibelanjakan / link_clicks, verified per row."""
    purdah = meta_rows[0]
    assert purdah.cpc == pytest.approx(10.36 / 78, abs=1e-9)
    meja = meta_rows[4]
    assert meja.cpc == pytest.approx(9.00 / 30, abs=1e-9)


def test_parse_meta_csv_ctr_is_zero_when_no_impressions():
    """A row with teraan == 0 must yield ctr == 0.0 (no div-by-zero)."""
    csv = "Nama iklan,Nama set iklan,Jenis bajet,Tarikh mula,Tarikh tamat,Hasil,Result indicator,Kos bagi setiap hasil,Jumlah dibelanjakan (MYR),Teraan,Capaian\n"
    csv += "VIDEO X,SIFAT,Using campaign budget,2026-06-14,2026-06-18,5,actions:link_click,0.10,0.50,0,0\n"
    tmp = FIXTURES_DIR / "_tmp_zero_impressions.csv"
    tmp.write_text(csv, encoding="utf-8")
    try:
        rows = parse_meta_csv(tmp)
        assert len(rows) == 1
        assert rows[0].ctr == 0.0
        assert rows[0].cpc == 0.10  # 0.50 / 5 — still has link_clicks
    finally:
        tmp.unlink()


def test_parse_meta_csv_cpc_is_zero_when_no_clicks(meta_rows):
    """VIDEO BANTAL: 5.00 spent, 0 link clicks -> cpc must be 0.0, not inf."""
    bantal = meta_rows[6]
    assert bantal.link_clicks == 0
    assert bantal.cpc == 0.0
    # Sanity: never NaN/inf — downstream rendering relies on finite floats
    import math
    assert math.isfinite(bantal.cpc)


def test_parse_meta_csv_ctr_in_unit_interval(meta_rows):
    """Spec invariant: ctr must be in [0, 1] for every row."""
    for row in meta_rows:
        assert 0.0 <= row.ctr <= 1.0


# ---------------------------------------------------------------------------
# parse_meta_csv — defensive paths
# ---------------------------------------------------------------------------

def test_parse_meta_csv_raises_on_missing_columns(tmp_path):
    """A CSV missing the required Bahasa Malaysia columns raises ValueError."""
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text(
        "Wrong,Headers,Here\nfoo,bar,baz\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        parse_meta_csv(bad_csv)
    msg = str(excinfo.value)
    assert "missing required columns" in msg.lower()
    assert "Nama iklan" in msg  # lists an expected header in the error


def test_parse_meta_csv_empty_rows_returns_empty_list(tmp_path):
    """A header-only CSV produces an empty list, not an error."""
    headers = (
        "Nama iklan,Nama set iklan,Jenis bajet,Tarikh mula,Tarikh tamat,"
        "Hasil,Result indicator,Kos bagi setiap hasil,"
        "Jumlah dibelanjakan (MYR),Teraan,Capaian\n"
    )
    empty = tmp_path / "empty.csv"
    empty.write_text(headers, encoding="utf-8")
    assert parse_meta_csv(empty) == []


def test_parse_meta_csv_file_not_found_raises(tmp_path):
    """A missing file surfaces FileNotFoundError, not something cryptic."""
    with pytest.raises(FileNotFoundError):
        parse_meta_csv(tmp_path / "does_not_exist.csv")


# ---------------------------------------------------------------------------
# summarize_campaign
# ---------------------------------------------------------------------------

def test_summarize_campaign_groups_by_nama_set_iklan(meta_rows):
    """Fixture has 2 ad sets: SIFAT (5 ads) and PRODUK (3 ads)."""
    summary = summarize_campaign(meta_rows)
    assert set(summary.keys()) == {"SIFAT", "PRODUK"}


def test_summarize_campaign_sifat_totals(meta_rows):
    """SIFAT: 5 ads, 505 link clicks, 29403 impressions, RM56.51 spent."""
    summary = summarize_campaign(meta_rows)
    sifat = summary["SIFAT"]
    assert sifat["ad_count"] == 5
    assert sifat["total_link_clicks"] == 78 + 65 + 42 + 120 + 200  # 505
    assert sifat["total_impressions"] == 6403 + 5500 + 4000 + 4500 + 9000  # 29403
    assert sifat["total_spend"] == pytest.approx(56.51, abs=1e-9)


def test_summarize_campaign_sifat_weighted_ctr(meta_rows):
    """SIFAT weighted CTR = total_link_clicks / total_impressions."""
    summary = summarize_campaign(meta_rows)
    sifat = summary["SIFAT"]
    expected = 505 / 29403
    assert sifat["weighted_ctr"] == pytest.approx(expected, abs=1e-9)


def test_summarize_campaign_sifat_weighted_cpc(meta_rows):
    """SIFAT weighted CPC = total_spend / total_link_clicks."""
    summary = summarize_campaign(meta_rows)
    sifat = summary["SIFAT"]
    expected = 56.51 / 505
    assert sifat["weighted_cpc"] == pytest.approx(expected, abs=1e-9)


def test_summarize_campaign_produk_totals(meta_rows):
    """PRODUK: 3 ads, 45 link clicks, 17000 impressions, RM21.50 spent."""
    summary = summarize_campaign(meta_rows)
    produk = summary["PRODUK"]
    assert produk["ad_count"] == 3
    assert produk["total_link_clicks"] == 30 + 15 + 0  # 45
    assert produk["total_impressions"] == 8000 + 6000 + 3000  # 17000
    assert produk["total_spend"] == pytest.approx(21.50, abs=1e-9)


def test_summarize_campaign_produk_weighted_cpc(meta_rows):
    """PRODUK weighted CPC = 21.50 / 45 ≈ 0.47778."""
    summary = summarize_campaign(meta_rows)
    produk = summary["PRODUK"]
    assert produk["weighted_cpc"] == pytest.approx(21.50 / 45, abs=1e-9)


def test_summarize_campaign_handles_zero_clicks_without_divbyzero(meta_rows):
    """Even with VIDEO BANTAL's 0 clicks, weighted_cpc must be finite."""
    summary = summarize_campaign(meta_rows)
    produk = summary["PRODUK"]  # PRODUK contains BANTAL
    import math
    assert math.isfinite(produk["weighted_cpc"])
    assert math.isfinite(produk["weighted_ctr"])


def test_summarize_campaign_empty_input_returns_empty_dict():
    assert summarize_campaign([]) == {}


# ---------------------------------------------------------------------------
# best_creative
# ---------------------------------------------------------------------------

def test_best_creative_returns_highest_ctr(meta_rows):
    """VIDEO KERUSI has CTR ≈ 0.0267, the highest in the fixture."""
    best = best_creative(meta_rows)
    assert best.nama_iklan == "VIDEO KERUSI"


def test_best_creative_empty_raises():
    with pytest.raises(ValueError):
        best_creative([])


def test_best_creative_single_row_returns_that_row(meta_rows):
    only = meta_rows[0]
    assert best_creative([only]) is only


# ---------------------------------------------------------------------------
# worst_creative
# ---------------------------------------------------------------------------

def test_worst_creative_returns_lowest_ctr_among_impressed(meta_rows):
    """VIDEO BANTAL has CTR = 0.0 with 3000 impressions -> worst."""
    worst = worst_creative(meta_rows)
    assert worst.nama_iklan == "VIDEO BANTAL"


def test_worst_creative_excludes_zero_impression_rows(meta_rows):
    """Even though BANTAL has CTR=0, a row with teraan=0 wouldn't beat it.
    Build a synthetic case: one ad with teraan=0 (excluded) and one with
    a real CTR > 0; the real one must win.
    """
    from modules.models import MetaAdRow

    rows = [
        MetaAdRow(
            nama_iklan="ZERO_IMP",
            nama_set_iklan="X",
            jenis_bajet="b",
            tarikh_mula=date(2026, 1, 1),
            tarikh_tamat=date(2026, 1, 2),
            hasil=0,
            result_indicator="actions:link_click",
            kos_bagi_setiap_hasil=0.0,
            jumlah_dibelanjakan=0.0,
            teraan=0,           # excluded
            capaian=0,
            link_clicks=0,
            ctr=0.0,            # would be "worst" if not excluded
            cpc=0.0,
        ),
        MetaAdRow(
            nama_iklan="REAL_LOW",
            nama_set_iklan="X",
            jenis_bajet="b",
            tarikh_mula=date(2026, 1, 1),
            tarikh_tamat=date(2026, 1, 2),
            hasil=10,
            result_indicator="actions:link_click",
            kos_bagi_setiap_hasil=0.1,
            jumlah_dibelanjakan=1.0,
            teraan=1000,        # real impressions
            capaian=900,
            link_clicks=10,
            ctr=0.01,           # the actual worst
            cpc=0.1,
        ),
    ]
    assert worst_creative(rows).nama_iklan == "REAL_LOW"


def test_worst_creative_all_zero_impressions_raises():
    from modules.models import MetaAdRow

    rows = [
        MetaAdRow(
            nama_iklan="X",
            nama_set_iklan="X",
            jenis_bajet="b",
            tarikh_mula=date(2026, 1, 1),
            tarikh_tamat=date(2026, 1, 2),
            hasil=0,
            result_indicator="actions:link_click",
            kos_bagi_setiap_hasil=0.0,
            jumlah_dibelanjakan=0.0,
            teraan=0,
            capaian=0,
            link_clicks=0,
            ctr=0.0,
            cpc=0.0,
        ),
    ]
    with pytest.raises(ValueError):
        worst_creative(rows)


def test_worst_creative_empty_raises():
    with pytest.raises(ValueError):
        worst_creative([])


# ---------------------------------------------------------------------------
# Cross-module invariants
# ---------------------------------------------------------------------------

def test_ctr_is_weighted_correctly_not_simple_average(meta_rows):
    """A simple arithmetic mean of per-ad CTR would be misleading.

    With SIFAT's 5 ads, the simple-mean CTR differs from the weighted CTR.
    Verify the function returned the *weighted* (pooled) version.
    """
    summary = summarize_campaign(meta_rows)
    sifat = summary["SIFAT"]

    sifat_rows = [r for r in meta_rows if r.nama_set_iklan == "SIFAT"]
    simple_mean = sum(r.ctr for r in sifat_rows) / len(sifat_rows)
    assert sifat["weighted_ctr"] != pytest.approx(simple_mean, rel=1e-3)
    # The weighted CTR must equal total_link_clicks / total_impressions
    assert sifat["weighted_ctr"] == pytest.approx(
        sum(r.link_clicks for r in sifat_rows)
        / sum(r.teraan for r in sifat_rows),
        abs=1e-9,
    )
