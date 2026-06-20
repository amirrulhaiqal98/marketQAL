"""
Constants and thresholds for Hermes AI.

Tweak the EPC thresholds here to retune the SCALE / HOLD / KILL
classifier without touching business logic.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# EPC classification thresholds (RM per click)
# ---------------------------------------------------------------------------

EPC_SCALE_THRESHOLD: float = 0.05   # >= this -> SCALE
EPC_KILL_THRESHOLD: float = 0.01    # <  this -> KILL (else HOLD)

CLASSIFICATIONS: frozenset[str] = frozenset({"SCALE", "HOLD", "KILL"})


# ---------------------------------------------------------------------------
# Telegram commands whitelist (per docs/DEVELOPMENT_RULES.md)
# ---------------------------------------------------------------------------

ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "/start",
    "/help",
    "/score",
    "/analyze_meta",
    "/analyze_shopee",
    "/insights",
})


# ---------------------------------------------------------------------------
# Channel normalization map (Shopee click referrer -> canonical)
# ---------------------------------------------------------------------------

CHANNEL_NORMALIZATION: dict[str, str] = {
    "Facebook": "Facebook",
    "Instagram": "Instagram",
    "WhatsApp": "WhatsApp",
    "Telegram": "Telegram",
    "Websites": "Websites",
    "Others": "Others",
}


# ---------------------------------------------------------------------------
# Sub-id sentinel for direct / untracked traffic
# ---------------------------------------------------------------------------

DIRECT_TRAFFIC_KEY: str = "DIRECT"


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------

UPLOADS_DIR: str = "data/uploads"
EXPORTS_DIR: str = "data/exports"
FIXTURES_DIR: str = "data/fixtures"


# ---------------------------------------------------------------------------
# MiniMax / API defaults
# ---------------------------------------------------------------------------

DEFAULT_MINIMAX_MODEL: str = "MiniMax-Text-01"
DEFAULT_MINIMAX_TEMPERATURE: float = 0.7
DEFAULT_MINIMAX_MAX_TOKENS: int = 1024
MINIMAX_TIMEOUT_SECONDS: float = 30.0
