"""
Telegram bot entry point for Hermes v1.

This module is the **only** place in the codebase that talks to the
Telegram Bot API. It registers six Phase-1 commands plus a single
document handler, holds per-chat state in memory (no DB, no persistence),
and delegates every piece of business logic to ``modules/``.

Design rules (from ``docs/DEVELOPMENT_RULES.md``):

* No HTTP, no LLM calls, no CSV parsing here.
* All state lives in :class:`SessionState`, stashed in
  ``context.user_data`` so python-telegram-bot manages its lifetime.
* All pure helpers (``split_score_args``, ``detect_shopee_csv_kind``,
  ``format_*``, ``truncate_for_telegram``) are exported and unit-tested
  without touching Telegram.

Phase-1 commands (per :data:`modules.constants.ALLOWED_COMMANDS`):

* ``/start``         welcome + reset session state
* ``/help``          list of commands
* ``/score``         score a Shopee product (MiniMax)
* ``/analyze_meta``  expect a Meta Ads CSV upload next
* ``/analyze_shopee`` expect a Shopee CSV upload next (auto-detected)
* ``/insights``      run the insight engine on the loaded data

Plus ``/cancel`` (clear pending-upload flag) and ``/clear`` (reset
session state) for convenience, plus a plain-text message handler that
consumes the interactive ``/score`` follow-up.

Environment:

* ``TELEGRAM_BOT_TOKEN`` (required) — BotFather token.
* ``MINIMAX_API_KEY`` (required by ``minimax.py``) — loaded at import time.

Run with::

    cd hermes-v1
    python bot.py
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from minimax import MiniMaxError, close_client
from modules.constants import ALLOWED_COMMANDS, UPLOADS_DIR
from modules.insight_engine import build_kpis, render_summary
from modules.meta_analyzer import parse_meta_csv
from modules.models import (
    MetaAdRow,
    ScoringResult,
    ShopeeClickRow,
    ShopeeCommissionRow,
)
from modules.product_scorer import async_score_product
from modules.shopee_analyzer import parse_click_csv, parse_commission_csv


# ``.env`` is also loaded by ``minimax.py``; calling it again is idempotent.
load_dotenv()


logger = logging.getLogger(__name__)


__all__ = [
    # Session + state
    "SessionState",
    # Pure helpers (exported for unit tests)
    "split_score_args",
    "detect_shopee_csv_kind",
    "format_score_message",
    "format_meta_summary",
    "format_uploads_required",
    "truncate_for_telegram",
    "sanitize_filename",
    "build_save_path",
    # Telegram handlers (public so Application wiring can be tested)
    "start_cmd",
    "help_cmd",
    "score_cmd",
    "analyze_meta_cmd",
    "analyze_shopee_cmd",
    "insights_cmd",
    "cancel_cmd",
    "clear_cmd",
    "document_handler",
    "text_handler",
    "error_handler",
    # Entry point
    "main",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TELEGRAM_MESSAGE_LIMIT: int = 4096
TELEGRAM_FILE_LIMIT_BYTES: int = 20 * 1024 * 1024  # 20 MB
_ALLOWED_UPLOAD_SUFFIXES: frozenset[str] = frozenset({".csv"})

# user_data keys — kept as constants to avoid typos scattered across handlers.
_AWAITING_KEY: str = "awaiting"
_SESSION_KEY: str = "session"

# Recognised "awaiting" states. Strings match the brainstorming spec exactly.
_AWAITING_META: str = "meta_csv"
_AWAITING_SHOPEE: str = "shopee_csv"
_AWAITING_SCORE: str = "score_args"

_TRUNCATION_MARKER: str = "\n\n[...truncated — full report too long for Telegram]"


# ---------------------------------------------------------------------------
# SessionState — per-chat in-memory state (no DB, no persistence)
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    """In-memory state for one Telegram chat.

    Phase 1 holds the last-loaded Meta + Shopee data so the user can
    upload three files in any order and then call ``/insights``. Nothing
    is persisted to disk (per Phase 1 scope guardrails).
    """
    meta_rows: list[MetaAdRow] = field(default_factory=list)
    click_rows: list[ShopeeClickRow] = field(default_factory=list)
    commission_rows: list[ShopeeCommissionRow] = field(default_factory=list)

    def has_meta(self) -> bool:
        return bool(self.meta_rows)

    def has_clicks(self) -> bool:
        return bool(self.click_rows)

    def has_commissions(self) -> bool:
        return bool(self.commission_rows)

    def has_full_dataset(self) -> bool:
        """True iff all three datasets have at least one row."""
        return self.has_meta() and self.has_clicks() and self.has_commissions()

    def has_shopee_partial(self) -> bool:
        return self.has_clicks() or self.has_commissions()

    def missing_for_insights(self) -> list[str]:
        """Return a list of human-readable descriptions of missing datasets."""
        missing: list[str] = []
        if not self.has_meta():
            missing.append("Meta CSV — /analyze_meta")
        if not self.has_clicks():
            missing.append("Shopee click CSV — /analyze_shopee")
        if not self.has_commissions():
            missing.append("Shopee commission CSV — /analyze_shopee")
        return missing

    def clear(self) -> None:
        self.meta_rows = []
        self.click_rows = []
        self.commission_rows = []

    def set_meta(self, rows: list[MetaAdRow]) -> None:
        self.meta_rows = list(rows)

    def set_clicks(self, rows: list[ShopeeClickRow]) -> None:
        self.click_rows = list(rows)

    def set_commissions(self, rows: list[ShopeeCommissionRow]) -> None:
        self.commission_rows = list(rows)


# ---------------------------------------------------------------------------
# Pure helpers — exported for unit tests, no Telegram / async / I/O
# ---------------------------------------------------------------------------

def split_score_args(args_text: str) -> tuple[str, float, str]:
    """Split ``"Title | Price | Category"`` into typed fields.

    Raises:
        ValueError: On wrong shape, empty title/category, non-numeric
            price, or negative price.
    """
    text = (args_text or "").strip()
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 3:
        raise ValueError(
            f"Expected 3 parts separated by '|', got {len(parts)}. "
            f"Format: Title | Price | Category"
        )
    title, price_text, category = parts
    if not title:
        raise ValueError("Title must be non-empty.")
    if not category:
        raise ValueError("Category must be non-empty.")
    try:
        price = float(price_text)
    except ValueError as exc:
        raise ValueError(
            f"Price must be a number, got {price_text!r}."
        ) from exc
    if price < 0:
        raise ValueError(f"Price must be >= 0, got {price}.")
    return title, price, category


def detect_shopee_csv_kind(path: Path) -> str:
    """Return ``"click"`` or ``"commission"`` based on CSV header line.

    Detection is intentionally simple — only the first line is read and
    matched case-insensitively. The Shopee click report has a column
    like ``"Click id"``; the commission report has ``"Order id"`` or
    ``"Order Status"``. Anything else raises :class:`ValueError`.

    Raises:
        ValueError: On unreadable file or unrecognised header.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            header_line = (f.readline() or "").lower()
    except OSError as exc:
        raise ValueError(f"Cannot read CSV header: {exc}") from exc

    if "click id" in header_line or "sub_id" in header_line:
        return "click"
    if "order id" in header_line or "order status" in header_line:
        return "commission"
    raise ValueError(
        "Unknown Shopee CSV: header doesn't match click or commission "
        f"schema. Header: {header_line.strip()!r}"
    )


def sanitize_filename(name: str) -> str:
    """Strip directory components and reject empty / dot-only names.

    Returns a safe filename component. Falls back to ``"upload"`` if the
    cleaned name is empty.
    """
    cleaned = Path(name or "").name  # strips directory traversal
    cleaned = cleaned.strip().lstrip(".")
    return cleaned or "upload"


def build_save_path(uploads_dir: Path, chat_id: int | str, original_name: str) -> Path:
    """Build a deterministic per-upload save path.

    Layout: ``<uploads_dir>/<chat_id>_<UTC-timestamp>_<safe-name>``. The
    timestamp has 1-second resolution; if a file already exists with the
    same name (rare — same chat uploading twice in <1s) the caller's
    filesystem layer will append a suffix or overwrite.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = sanitize_filename(original_name)
    return uploads_dir / f"{chat_id}_{ts}_{safe}"


def format_score_message(
    result: ScoringResult, *, title: str, price: float, category: str
) -> str:
    """Render a :class:`ScoringResult` as Telegram-friendly text."""
    lines: list[str] = [
        f"📊 Clickability Score: {result.score}/100",
        "",
        f"Title:    {title}",
        f"Price:    RM {price:.2f}",
        f"Category: {category}",
        "",
        "Facebook hooks:",
        *[f"  {i}. {h}" for i, h in enumerate(result.fb_hooks, 1)],
        "",
        "Threads hooks:",
        *[f"  {i}. {h}" for i, h in enumerate(result.threads_hooks, 1)],
        "",
        f"Why: {result.reasoning}",
    ]
    return "\n".join(lines)


def format_meta_summary(rows: list[MetaAdRow]) -> str:
    """Render a confirmation message after a successful Meta upload."""
    if not rows:
        return "✅ Meta CSV loaded but contained 0 parseable rows."
    total_spend = sum(float(r.jumlah_dibelanjakan) for r in rows)
    total_clicks = sum(int(r.link_clicks) for r in rows)
    ad_sets = sorted({r.nama_set_iklan for r in rows})
    earliest = min(r.tarikh_mula for r in rows)
    latest = max(r.tarikh_tamat for r in rows)
    return (
        f"✅ Meta CSV loaded: {len(rows)} rows\n"
        f"   Spend:    RM {total_spend:.2f}\n"
        f"   Clicks:   {total_clicks}\n"
        f"   Ad-sets:  {', '.join(ad_sets)}\n"
        f"   Period:   {earliest} → {latest}\n\n"
        f"Next: upload Shopee CSVs via /analyze_shopee, then /insights."
    )


def format_uploads_required(state: SessionState) -> str:
    """Render the ``/insights`` missing-data message."""
    missing = state.missing_for_insights()
    if not missing:
        return "All datasets loaded."
    return (
        "❌ /insights needs all three uploads first:\n\n"
        + "\n".join(f"• {m}" for m in missing)
    )


def truncate_for_telegram(
    text: str, limit: int = TELEGRAM_MESSAGE_LIMIT
) -> str:
    """Truncate text so it fits Telegram's per-message char limit.

    Appends a ``[...truncated]`` marker if clipped. Returns the input
    unchanged when it already fits.
    """
    if len(text) <= limit:
        return text
    keep = limit - len(_TRUNCATION_MARKER)
    if keep < 1:
        # Defensive: if the marker alone is bigger than the limit, hard-truncate.
        return text[:limit]
    return text[:keep] + _TRUNCATION_MARKER


# ---------------------------------------------------------------------------
# Internal session helper
# ---------------------------------------------------------------------------

def _get_session(context: ContextTypes.DEFAULT_TYPE) -> SessionState:
    """Return the :class:`SessionState` for this chat, creating if absent."""
    state = context.user_data.get(_SESSION_KEY)
    if state is None:
        state = SessionState()
        context.user_data[_SESSION_KEY] = state
    return state


def _ensure_uploads_dir() -> Path:
    """Create :data:`UPLOADS_DIR` if missing and return it as a :class:`Path`."""
    p = Path(UPLOADS_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def start_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/start`` — welcome + reset session state."""
    if not update.message:
        return
    context.user_data[_AWAITING_KEY] = None
    _get_session(context).clear()
    await update.message.reply_text(
        "🛰 Hermes ready — Affiliate Traffic Intelligence.\n"
        "Session cleared. /help for the full command list."
    )


async def help_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/help`` — list all available commands."""
    if not update.message:
        return
    await update.message.reply_text(
        "🛰 Hermes — Affiliate Traffic Intelligence\n"
        "\n"
        "/start         Welcome + reset session\n"
        "/help          Show this message\n"
        "/score         Score a product\n"
        "               usage: /score Title | Price | Category\n"
        "/analyze_meta  Upload Meta Ads CSV (Bahasa headers)\n"
        "/analyze_shopee Upload Shopee CSV (click or commission)\n"
        "/insights      Run insight engine on loaded data\n"
        "/cancel        Cancel pending file upload\n"
        "/clear         Reset session state\n"
    )


async def score_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/score [Title | Price | Category]``."""
    if not update.message:
        return

    raw_args = " ".join(context.args or []).strip()
    if not raw_args:
        context.user_data[_AWAITING_KEY] = _AWAITING_SCORE
        await update.message.reply_text(
            "Send the product details on the next line:\n"
            "Title | Price | Category\n"
            "Example: Wireless Earbuds | 29.90 | Electronics\n\n"
            "Or run `/score Title | Price | Category` directly.\n"
            "/cancel to abort."
        )
        return

    await _run_score(update, context, raw_args)


async def _run_score(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_args: str,
) -> None:
    """Shared scoring pipeline used by both ``/score`` and the text follow-up."""
    assert update.message is not None

    try:
        title, price, category = split_score_args(raw_args)
    except ValueError as exc:
        await update.message.reply_text(
            f"❌ {exc}\nFormat: Title | Price | Category"
        )
        return

    chat = update.effective_chat
    if chat:
        try:
            await context.application.bot.send_chat_action(
                chat.id, ChatAction.TYPING
            )
        except Exception:  # noqa: BLE001 — chat action is best-effort
            pass

    try:
        result = await async_score_product(title, price, category)
    except MiniMaxError as exc:
        logger.exception("MiniMax error in /score")
        await update.message.reply_text(
            f"❌ Scoring failed (API): {exc}"
        )
        return
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return

    text = format_score_message(
        result, title=title, price=price, category=category
    )
    await update.message.reply_text(truncate_for_telegram(text))


async def analyze_meta_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/analyze_meta`` — expect a Meta CSV upload next."""
    if not update.message:
        return
    context.user_data[_AWAITING_KEY] = _AWAITING_META
    await update.message.reply_text(
        "📤 Send the Meta Ads CSV export.\n"
        "Accepted: .csv (max 20 MB).\n"
        "Send /cancel to abort."
    )


async def analyze_shopee_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/analyze_shopee`` — expect a Shopee CSV (auto-detect on upload)."""
    if not update.message:
        return
    context.user_data[_AWAITING_KEY] = _AWAITING_SHOPEE
    state = _get_session(context)
    if state.has_shopee_partial():
        # Remind the user which Shopee dataset is still missing.
        needed: list[str] = []
        if not state.has_clicks():
            needed.append("click")
        if not state.has_commissions():
            needed.append("commission")
        await update.message.reply_text(
            f"📤 Send the Shopee {', '.join(needed)} CSV.\n"
            "Accepted: .csv (max 20 MB).\n"
            "I'll auto-detect from the header.\n"
            "Send /cancel to abort."
        )
    else:
        await update.message.reply_text(
            "📤 Send the Shopee CSV — click report or commission report.\n"
            "I'll auto-detect from the header.\n"
            "Accepted: .csv (max 20 MB).\n"
            "Send /cancel to abort."
        )


async def insights_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/insights`` — run the insight engine on the loaded data."""
    if not update.message:
        return
    state = _get_session(context)
    if not state.has_full_dataset():
        await update.message.reply_text(format_uploads_required(state))
        return

    chat = update.effective_chat
    if chat:
        try:
            await context.application.bot.send_chat_action(
                chat.id, ChatAction.TYPING
            )
        except Exception:  # noqa: BLE001 — best-effort UX
            pass

    kpis = build_kpis(state.meta_rows, state.click_rows, state.commission_rows)
    text = render_summary(kpis)
    await update.message.reply_text(truncate_for_telegram(text))


async def cancel_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/cancel`` — clear any pending-upload flag."""
    if not update.message:
        return
    was_waiting = context.user_data.pop(_AWAITING_KEY, None)
    if was_waiting:
        await update.message.reply_text(
            f"Cancelled (was waiting for: {was_waiting!r})."
        )
    else:
        await update.message.reply_text("Nothing to cancel.")


async def clear_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/clear`` — reset all session state."""
    if not update.message:
        return
    context.user_data[_AWAITING_KEY] = None
    _get_session(context).clear()
    await update.message.reply_text(
        "🧹 Session cleared. All loaded datasets forgotten."
    )


async def document_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle uploaded documents — dispatch based on ``user_data['awaiting']``."""
    if not update.message or not update.message.document:
        return
    doc = update.message.document

    if doc.file_size is not None and doc.file_size > TELEGRAM_FILE_LIMIT_BYTES:
        size_mb = doc.file_size / (1024 * 1024)
        await update.message.reply_text(
            f"❌ File too large ({size_mb:.1f} MB). "
            "Telegram bots cap downloads at 20 MB."
        )
        return

    suffix = Path(doc.file_name or "").suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
        await update.message.reply_text(
            f"❌ Unsupported file type: {suffix or '?'}. "
            f"Send one of: {sorted(_ALLOWED_UPLOAD_SUFFIXES)}."
        )
        return

    awaiting = context.user_data.get(_AWAITING_KEY)
    if awaiting not in (_AWAITING_META, _AWAITING_SHOPEE):
        await update.message.reply_text(
            "❌ I wasn't expecting a file. Use /analyze_meta or "
            "/analyze_shopee first."
        )
        return

    chat = update.effective_chat
    if chat:
        try:
            await context.application.bot.send_chat_action(
                chat.id, ChatAction.TYPING
            )
        except Exception:  # noqa: BLE001 — best-effort UX
            pass

    uploads_dir = _ensure_uploads_dir()
    save_path = build_save_path(
        uploads_dir,
        chat.id if chat else "unknown",
        doc.file_name or "upload.csv",
    )

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(save_path)
    except Exception as exc:  # noqa: BLE001 — Telegram-side errors are many
        logger.exception("Failed to download document")
        await update.message.reply_text(
            f"❌ Failed to download file: {exc}"
        )
        return

    try:
        await _dispatch_upload(update, context, awaiting, save_path)
    except MiniMaxError as exc:
        await update.message.reply_text(f"❌ {exc}")
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
    except Exception as exc:  # noqa: BLE001 — pandas / parsing errors vary
        logger.exception("Failed to process upload %s", save_path)
        await update.message.reply_text(
            f"❌ Failed to parse file: {type(exc).__name__}: {exc}"
        )


async def _dispatch_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    awaiting: str,
    path: Path,
) -> None:
    """Parse the downloaded file and update session state."""
    assert update.message is not None
    state = _get_session(context)

    if awaiting == _AWAITING_META:
        rows = parse_meta_csv(path)
        state.set_meta(rows)
        context.user_data[_AWAITING_KEY] = None
        await update.message.reply_text(format_meta_summary(rows))
        return

    if awaiting == _AWAITING_SHOPEE:
        kind = detect_shopee_csv_kind(path)
        if kind == "click":
            rows = parse_click_csv(path)
            state.set_clicks(rows)
            msg = f"✅ Shopee click CSV loaded: {len(rows)} click rows."
            next_kind = "commission"
        else:
            rows = parse_commission_csv(path)
            state.set_commissions(rows)
            realized = sum(1 for r in rows if r.is_realized)
            msg = (
                f"✅ Shopee commission CSV loaded: {len(rows)} rows "
                f"({realized} completed, "
                f"{len(rows) - realized} pending)."
            )
            next_kind = "click"

        if state.click_rows and state.commission_rows:
            context.user_data[_AWAITING_KEY] = None
            await update.message.reply_text(
                msg + "\n\nBoth Shopee CSVs loaded. /insights when ready."
            )
        else:
            # Keep awaiting set so the next upload goes to the missing slot.
            await update.message.reply_text(
                msg
                + f"\n\nNow send the Shopee {next_kind} CSV "
                "(or /cancel to stop here)."
            )
        return

    # Defensive: shouldn't reach here — document_handler filters awaiting.
    await update.message.reply_text(
        f"❌ Unexpected awaiting state: {awaiting!r}."
    )


async def text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Plain-text handler — only active during the interactive ``/score`` flow.

    Other plain-text messages (e.g. someone typing in a group chat) are
    silently ignored so the bot doesn't become a chatterbox.
    """
    if not update.message or not update.message.text:
        return
    if context.user_data.get(_AWAITING_KEY) != _AWAITING_SCORE:
        return

    context.user_data[_AWAITING_KEY] = None
    await _run_score(update, context, update.message.text)


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log any uncaught exception from a handler. Telegram will show the
    user a generic error by default — this only ensures we have a record.
    """
    logger.exception(
        "Unhandled exception in handler (update=%s): %s",
        update, context.error,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    """Build the :class:`Application` and start long-polling."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Set it in hermes-v1/.env."
        )

    app = Application.builder().token(token).build()

    # Command handlers — order doesn't matter, but keep alphabetical for
    # readability. /cancel and /clear are non-spec convenience commands.
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("score", score_cmd))
    app.add_handler(CommandHandler("analyze_meta", analyze_meta_cmd))
    app.add_handler(CommandHandler("analyze_shopee", analyze_shopee_cmd))
    app.add_handler(CommandHandler("insights", insights_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))

    # Plain-text handler for the interactive /score follow-up. Sits
    # BEFORE the document handler so it has a chance to fire first; in
    # practice MessageHandler priority is by registration order.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    # Document handler last so commands are not shadowed.
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))

    app.add_error_handler(error_handler)

    logger.info(
        "Hermes bot starting (allowed commands=%s)",
        sorted(ALLOWED_COMMANDS),
    )
    try:
        await app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        await close_client()
        logger.info("Hermes bot stopped; MiniMax client closed.")


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    asyncio_run = __import__("asyncio").run
    asyncio_run(main())
