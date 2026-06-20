"""
Dataclasses for Hermes AI.

Field names mirror CSV column headers (snake_cased) so the mapping layer
from pandas DataFrame -> dataclass is obvious and grep-able.

Source schemas (real, from user):
  - Meta Ads CSV: Bahasa Malaysia headers (Nama iklan, Hasil, ...)
  - Shopee Click Report CSV: English headers (Click id, Click Time, Sub_id, Referrer)
  - Shopee Commission Report CSV: English headers (Order id, Order Status, Affiliate Net Commission(RM), ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Meta Ads (Malay-header export from Facebook Ads Manager)
# ---------------------------------------------------------------------------

@dataclass
class MetaAdRow:
    """One row from the Meta Ads CSV export.

    All field names are snake_cased versions of the Bahasa Malaysia
    CSV headers. Derived metrics (link_clicks, ctr, cpc) are computed
    in meta_analyzer.parse_meta_csv.
    """
    nama_iklan: str               # "VIDEO GLOVE"
    nama_set_iklan: str           # "SIFAT"
    jenis_bajet: str              # "Using campaign budget"
    tarikh_mula: date
    tarikh_tamat: date
    hasil: int                    # 78 (link clicks — the primary "Result" metric)
    result_indicator: str         # "actions:link_click"
    kos_bagi_setiap_hasil: float  # 0.13282051 (cost per result = CPC)
    jumlah_dibelanjakan: float    # 10.36 (MYR spent)
    teraan: int                   # 6403 (impressions)
    capaian: int                  # 6014 (reach)
    # Derived
    link_clicks: int = 0          # alias for `hasil` when result_indicator == actions:link_click
    ctr: float = 0.0              # link_clicks / teraan
    cpc: float = 0.0              # jumlah_dibelanjakan / link_clicks


# ---------------------------------------------------------------------------
# Shopee Click Report
# ---------------------------------------------------------------------------

@dataclass
class ShopeeClickRow:
    """One row from the Shopee Website Click Report CSV."""
    click_id: str
    click_time: datetime
    click_region: str             # "Malaysia", "Indonesia", "Philippines", ...
    sub_id_raw: str               # "FB----", "produkFB----", "----"
    referrer: str                 # "Facebook", "Instagram", "WhatsApp", ...
    # Derived
    campaign_key: str = ""        # "FB" | "produkFB" | "DIRECT"


# ---------------------------------------------------------------------------
# Shopee Commission Report
# ---------------------------------------------------------------------------

@dataclass
class ShopeeCommissionRow:
    """One row from the Shopee Affiliate Commission Report CSV.

    Note: a single `conversion_id` may produce multiple rows (multi-item
    orders across different shops). Group by conversion_id when needed.
    Only rows where order_status == "Completed" count toward realized
    commission / EPC.
    """
    order_id: str
    order_status: str             # "Pending" | "Completed"
    conversion_id: str
    order_time: Optional[datetime]
    complete_time: Optional[datetime]
    click_time: datetime
    shop_name: str
    item_name: str
    price_rm: float
    qty: int
    purchase_value_rm: float
    affiliate_net_commission_rm: float  # canonical commission number
    sub_id1: str                  # "FB" | "produkFB" | ""
    channel: str                  # "Facebook" | "WhatsApp" | "Websites"
    # Derived
    campaign_key: str = ""
    is_realized: bool = False     # True iff order_status == "Completed"


# ---------------------------------------------------------------------------
# Aggregated KPI (one per campaign_key)
# ---------------------------------------------------------------------------

@dataclass
class CampaignKPI:
    """Joined KPI snapshot for one campaign (e.g. "FB", "produkFB", "DIRECT")."""
    campaign_key: str
    channel: str                  # dominant referrer
    total_clicks: int
    total_commission_rm: float    # Completed orders only
    epc: float                    # commission / clicks
    meta_spend_rm: Optional[float] = None
    meta_link_clicks: Optional[int] = None
    meta_ctr: Optional[float] = None
    meta_cpc: Optional[float] = None
    classification: str = "HOLD"  # "SCALE" | "HOLD" | "KILL"


# ---------------------------------------------------------------------------
# Product scoring output
# ---------------------------------------------------------------------------

@dataclass
class ScoringResult:
    """Output of the /score command.

    Parsed from a MiniMax JSON response. Fields match the
    PRODUCT_SCORING_PROMPT contract in prompts.py.
    """
    score: int                                          # 0–100 clickability score
    fb_hooks: list[str] = field(default_factory=list)    # 3 Facebook hook variants
    threads_hooks: list[str] = field(default_factory=list)  # 3 Threads hook variants
    reasoning: str = ""                                 # 2–3 sentence justification
