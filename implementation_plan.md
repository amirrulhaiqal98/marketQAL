# Implementation Plan — Hermes AI (Affiliate Traffic Intelligence)

[Overview]
Build a Phase-1 Telegram bot named Hermes that ingests Meta Ads CSVs and Shopee Affiliate click/commission CSVs, computes EPC and traffic-quality KPIs, and emits structured SCALE/HOLD/KILL insights — without persisting any data, without ML, and without any Phase-2 feature creep.

This is a greenfield Python 3.13 project rooted in `hermes-v1/`. The workspace currently holds only the brainstorming spec (`brainstorming-1.0.md`). The architecture follows a strict separation: `bot.py` handles Telegram I/O only; `minimax.py` wraps MiniMax HTTP calls; `prompts.py` owns prompt strings; `modules/` owns all business logic. The build order mirrors the brainstorming doc: Telegram skeleton → MiniMax wrapper → Product Scorer → Meta Analyzer → Shopee Analyzer → EPC Calculator → Insight Engine.

User has confirmed: (a) Telegram bot token + MiniMax API key are ready (real `.env`, not mocks); (b) python-telegram-bot v20+ (async) is the chosen framework; (c) real CSV exports from a CBO campaign will be used as live test data.

[Types]
Define these Python `dataclass`es / `TypedDict`s in a single file `modules/models.py` so all downstream modules share the same shapes. Every field name mirrors CSV column headers exactly (snake_cased) to make the mapping layer obvious.

```
@dataclass
class MetaAdRow:
    nama_iklan: str                 # "VIDEO GLOVE"
    nama_set_iklan: str             # "SIFAT"
    jenis_bajet: str                # "Using campaign budget"
    tarikh_mula: date               # 2026-06-14
    tarikh_tamat: date              # 2026-06-18
    hasil: int                      # 78 (link clicks)
    result_indicator: str           # "actions:link_click"
    kos_bagi_setiap_hasil: float    # 0.13282051 (CPC)
    jumlah_dibelanjakan: float      # 10.36 (MYR)
    teraan: int                     # 6403 (impressions)
    capaian: int                    # 6014 (reach)
    link_clicks: int                # derived == hasil
    ctr: float                      # derived == link_clicks / teraan
    cpc: float                      # derived == jumlah_dibelanjakan / link_clicks

@dataclass
class ShopeeClickRow:
    click_id: str
    click_time: datetime
    click_region: str               # "Malaysia", "Indonesia", ...
    sub_id_raw: str                 # "FB----" or "produkFB----" or "----"
    referrer: str                   # "Facebook", "Instagram", "WhatsApp", "Telegram", "Others"
    campaign_key: str               # DERIVED: "FB" or "produkFB" or "DIRECT"

@dataclass
class ShopeeCommissionRow:
    order_id: str
    order_status: str               # "Pending" | "Completed" — only Completed counts for EPC
    conversion_id: str              # grouping key for multi-item orders
    order_time: Optional[datetime]
    complete_time: Optional[datetime]
    click_time: datetime
    shop_name: str
    item_name: str
    price_rm: float
    qty: int
    purchase_value_rm: float
    affiliate_net_commission_rm: float  # canonical commission number
    sub_id1: str                    # "FB" or "produkFB" or ""
    channel: str                    # "Facebook", "WhatsApp", "Websites"
    campaign_key: str               # DERIVED: same logic as click row

@dataclass
class CampaignKPI:
    campaign_key: str               # "FB" | "produkFB" | "DIRECT"
    channel: str                    # "Facebook" | "Instagram" | "WhatsApp" | ...
    total_clicks: int
    total_commission_rm: float      # Completed orders only
    epc: float                      # commission / clicks
    meta_spend_rm: Optional[float]  # joined from Meta export by campaign_key
    meta_link_clicks: Optional[int]
    meta_ctr: Optional[float]
    meta_cpc: Optional[float]
    classification: str             # "SCALE" | "HOLD" | "KILL"

@dataclass
class ScoringResult:
    score: int                      # 0–100 clickability score
    fb_hooks: list[str]             # 3 Facebook hook variants
    threads_hooks: list[str]        # 3 Threads hook variants
    reasoning: str                  # 2–3 sentence justification
```

**Validation rules:**
- `MetaAdRow.ctr` must be in [0, 1]; reject rows where `teraan == 0`.
- `CampaignKPI.epc` is `0.0` when `total_clicks == 0` (do not divide by zero).
- `ShopeeCommissionRow` rows with `order_status != "Completed"` are parsed but flagged `is_realized=False` and excluded from commission totals (but still counted in clicks attribution if needed).
- `campaign_key` derivation rule (shared by click + commission modules): strip trailing `----` from `sub_id_raw` / `sub_id1`. Empty → `"DIRECT"`. (Confirmed from user's real data: `FB----` → `FB`, `produkFB----` → `produkFB`, `----` → `DIRECT`.)

**Constants/enums** (in `modules/constants.py`):
```
EPC_SCALE_THRESHOLD = 0.05    # RM per click → SCALE
EPC_KILL_THRESHOLD = 0.01     # below → KILL
CLASSIFICATIONS = {"SCALE", "HOLD", "KILL"}
ALLOWED_COMMANDS = {"/start", "/help", "/score", "/analyze_meta", "/analyze_shopee", "/insights"}
```

[Files]
Create the directory tree exactly per the brainstorming spec, with these files. All paths are relative to `/Users/amirrulhaiqal/BMAD-Projects/marketQal/hermes-v1/`.

**Files to create:**

| Path | Purpose |
|------|---------|
| `hermes-v1/bot.py` | Telegram entry point. Registers 6 commands + document handler. No business logic. |
| `hermes-v1/minimax.py` | Async HTTP client for MiniMax API (chat completions). Handles retries, timeouts, error mapping. |
| `hermes-v1/prompts.py` | All prompt templates as module-level constants: `PRODUCT_SCORING_PROMPT`, `META_ANALYSIS_PROMPT`, `INSIGHT_PROMPT`. |
| `hermes-v1/requirements.txt` | Pinned deps. |
| `hermes-v1/.env.example` | Template: `TELEGRAM_BOT_TOKEN=`, `MINIMAX_API_KEY=`, `MINIMAX_BASE_URL`, `MINIMAX_MODEL`. |
| `hermes-v1/.env` | Real values (user provides, gitignored). |
| `hermes-v1/.gitignore` | `.env`, `data/uploads/*`, `__pycache__/`, `.venv/`, `*.pyc`, `.pytest_cache/`. |
| `hermes-v1/README.md` | Quickstart, env setup, command list, sample workflow. |
| `hermes-v1/hermes.log` | Runtime log (gitignored, auto-created). |
| `hermes-v1/modules/__init__.py` | Empty marker. |
| `hermes-v1/modules/models.py` | All dataclasses defined in [Types]. |
| `hermes-v1/modules/constants.py` | Thresholds, allowed commands, channel normalization map. |
| `hermes-v1/modules/product_scorer.py` | Wraps MiniMax call; parses JSON response into `ScoringResult`. |
| `hermes-v1/modules/meta_analyzer.py` | Reads Meta CSV → `list[MetaAdRow]` → KPI summary. |
| `hermes-v1/modules/shopee_analyzer.py` | Reads click + commission CSVs → `list[ShopeeClickRow]` + `list[ShopeeCommissionRow]` → aggregate by `campaign_key`. |
| `hermes-v1/modules/epc_calculator.py` | Pure function: `(clicks, commission) → float`. Unit-testable, no I/O. |
| `hermes-v1/modules/insight_engine.py` | Combines outputs from meta + shopee + epc → `list[CampaignKPI]` with SCALE/HOLD/KILL classification. |
| `hermes-v1/data/uploads/.gitkeep` | Placeholder; runtime files stored here. |
| `hermes-v1/data/exports/.gitkeep` | Placeholder for future report generation (Phase 2). |
| `hermes-v1/data/fixtures/meta_sample.csv` | 5–10 rows synthetic Meta export (Malay headers) for tests. |
| `hermes-v1/data/fixtures/shopee_click_sample.csv` | 20–30 click rows matching user's real schema (`FB----`, `produkFB----`, `----`). |
| `hermes-v1/data/fixtures/shopee_commission_sample.csv` | 10–15 commission rows including one Pending and one Completed multi-item order. |
| `hermes-v1/docs/README.md` | Copy of brainstorming overview. |
| `hermes-v1/docs/AGENTS.md` | Copy of brainstorming agent rules. |
| `hermes-v1/docs/DEVELOPMENT_RULES.md` | Phase-1 scope guardrails (no DB, no ML, no multi-agent). |
| `hermes-v1/tests/__init__.py` | Empty marker. |
| `hermes-v1/tests/conftest.py` | Pytest fixtures. |
| `hermes-v1/tests/test_meta.py` | Validates `meta_analyzer.parse_csv()` against fixture. |
| `hermes-v1/tests/test_shopee.py` | Validates `shopee_analyzer.parse_click_csv()` + `parse_commission_csv()`. |
| `hermes-v1/tests/test_epc.py` | Validates `epc_calculator.compute()`. |
| `hermes-v1/tests/test_insight.py` | Validates `insight_engine.classify()`. |

**Files to modify:** none — greenfield project.

**Files to delete/move:** none — existing `brainstorming-1.0.md` stays at repo root as historical reference.

**Config updates:** `requirements.txt` pinned versions; `.env.example` documents all 4 env vars with safe defaults.

[Functions]
All new functions are async unless pure/utility. Async functions use `httpx.AsyncClient` or `python-telegram-bot`'s `Application`/`ContextTypes`.

**`bot.py` — 6 command handlers + 1 document handler:**

| Function | Signature | Purpose |
|----------|-----------|---------|
| `main()` | `async def main() -> None` | Build `Application`, register handlers, call `run_polling()`. Entry point. |
| `start_cmd` | `async def(update, context) -> None` | Reply: "Hermes ready. /help for commands." |
| `help_cmd` | `async def(update, context) -> None` | Reply: list of 6 allowed commands. |
| `score_cmd` | `async def(update, context) -> None` | Prompt user to send `/score <title> | <price> | <category>` or reply with text after `/score`. |
| `analyze_meta_cmd` | `async def(update, context) -> None` | Set `context.user_data["awaiting"] = "meta_csv"`, prompt upload. |
| `analyze_shopee_cmd` | `async def(update, context) -> None` | Set `context.user_data["awaiting"] = "shopee_csv"`, prompt upload. |
| `insights_cmd` | `async def(update, context) -> None` | Triggers `insight_engine` on last-loaded Meta + Shopee data in memory. |
| `document_handler` | `async def(update, context) -> None` | Reads `.csv` / `.xlsx`, saves to `data/uploads/<chat_id>_<timestamp>.<ext>`, dispatches based on `user_data["awaiting"]`. |

**`minimax.py` — 4 functions:**

| Function | Signature | Purpose |
|----------|-----------|---------|
| `async_minimax_chat` | `async def(prompt: str, *, system: str = "", temperature: float = 0.7, max_tokens: int = 1024) -> str` | POST to `/chat/completions`, return assistant text. |
| `_build_payload` | `def(prompt: str, system: str, temperature: float, max_tokens: int) -> dict` | Build request body. Pure. |
| `_handle_response` | `async def(response: httpx.Response) -> str` | Raise on non-2xx with body excerpt; extract `choices[0].message.content`. |
| `get_client` | `def() -> httpx.AsyncClient` | Singleton client with 30s timeout, base URL from env. |

**`product_scorer.py` — 2 functions:**

| Function | Signature | Purpose |
|----------|-----------|---------|
| `async_score_product` | `async def(title: str, price: float, category: str) -> ScoringResult` | Calls MiniMax with `PRODUCT_SCORING_PROMPT`, parses JSON to `ScoringResult`. |
| `_parse_scoring_response` | `def(raw: str) -> ScoringResult` | Strip markdown fences, `json.loads`, validate field presence. |

**`meta_analyzer.py` — 4 functions:**

| Function | Signature | Purpose |
|----------|-----------|---------|
| `parse_meta_csv` | `def(path: str \| Path) -> list[MetaAdRow]` | Read CSV with Malay headers via `pandas.read_csv`, map to dataclass. Compute CTR/CPC. |
| `summarize_campaign` | `def(rows: list[MetaAdRow]) -> dict[str, float]` | Aggregate by `nama_set_iklan` (CBO grouping): total spend, total link_clicks, weighted CTR, weighted CPC. |
| `best_creative` | `def(rows: list[MetaAdRow]) -> MetaAdRow` | Return row with highest CTR. |
| `worst_creative` | `def(rows: list[MetaAdRow]) -> MetaAdRow` | Return row with lowest CTR (excluding zero-impression rows). |

**`shopee_analyzer.py` — 5 functions:**

| Function | Signature | Purpose |
|----------|-----------|---------|
| `parse_click_csv` | `def(path: str \| Path) -> list[ShopeeClickRow]` | Read CSV, derive `campaign_key`. |
| `parse_commission_csv` | `def(path: str \| Path) -> list[ShopeeCommissionRow]` | Read CSV, parse datetimes, derive `campaign_key`. |
| `_derive_campaign_key` | `def(sub_id_raw: str) -> str` | Strip trailing `----`; empty → `"DIRECT"`. Pure. |
| `aggregate_by_campaign` | `def(clicks: list[ShopeeClickRow], commissions: list[ShopeeCommissionRow]) -> dict[str, dict]` | Group both by `campaign_key`. |
| `top_channel` | `def(clicks: list[ShopeeClickRow]) -> tuple[str, int]` | Return `(referrer, count)` of most-clicked channel. |

**`epc_calculator.py` — 2 pure functions:**

| Function | Signature | Purpose |
|----------|-----------|---------|
| `compute` | `def(clicks: int, commission_rm: float) -> float` | Returns `commission_rm / clicks` if clicks > 0 else `0.0`. Rounded to 6 decimals. |
| `compute_bulk` | `def(rows: list[tuple[int, float]]) -> list[float]` | Vectorized wrapper. |

**`insight_engine.py` — 3 functions:**

| Function | Signature | Purpose |
|----------|-----------|---------|
| `classify_epc` | `def(epc: float) -> str` | Returns `"SCALE"` if ≥ 0.05, `"KILL"` if < 0.01, else `"HOLD"`. Pure. |
| `build_kpis` | `def(meta_rows: list[MetaAdRow], click_rows: list[ShopeeClickRow], commission_rows: list[ShopeeCommissionRow]) -> list[CampaignKPI]` | Joins all three by `campaign_key`, builds KPI list. |
| `render_summary` | `def(kpis: list[CampaignKPI]) -> str` | Produces MarkdownV2-safe Telegram message. |

**Removed functions:** none (greenfield).

[Classes]
Single class to introduce for centralized state during a single user session. No persistence (Phase 1).

| Class | File | Purpose |
|-------|------|---------|
| `SessionState` | `bot.py` | Holds `last_meta_rows`, `last_click_rows`, `last_commission_rows` in `context.user_data`. Methods: `set_meta(rows)`, `set_clicks(rows)`, `set_commissions(rows)`, `has_full_dataset() -> bool`, `clear()`. No inheritance. |

No other classes. Logic lives in module-level functions for testability.

[Dependencies]
Add 6 runtime + 3 dev dependencies to `hermes-v1/requirements.txt`, all pinned to known-good versions on Python 3.13:

```
# Runtime
python-telegram-bot[job-queue]==21.6
httpx==0.27.2
pandas==2.2.3
openpyxl==3.1.5
python-dotenv==1.0.1
python-dateutil==2.9.0.post0

# Dev
pytest==8.3.3
pytest-asyncio==0.24.0
ruff==0.6.8
```

**Why each:**
- `python-telegram-bot[job-queue]==21.6` — async v20+ API, includes `job-queue` extra for future scheduled reports.
- `httpx==0.27.2` — async HTTP client for MiniMax; supports timeouts and connection pooling natively.
- `pandas==2.2.3` — robust CSV/XLSX parsing for Malay-header Meta exports and large Shopee click reports.
- `openpyxl==3.1.5` — `.xlsx` support for Shopee exports that aren't CSV.
- `python-dotenv==1.0.1` — `.env` loading at startup.
- `python-dateutil==2.9.0.post0` — robust parsing of mixed datetime formats (`2026-06-18 11:12:59`).
- `pytest`, `pytest-asyncio`, `ruff` — testing and linting.

**No system-level deps.** No Docker, no DB, no cloud SDK.

**Version note:** `python-telegram-bot 21.x` is the latest v20+ line as of mid-2025 and supports Python 3.13. The async `Application` builder pattern is used: `Application.builder().token(TOKEN).build()`.

[Testing]
Use `pytest` + `pytest-asyncio`. Three test files (per brainstorming doc) plus one added `test_insight.py` for the classification thresholds (critical to correctness, warrants explicit coverage).

**Test approach:**
- Pure unit tests for `epc_calculator.compute()` and `insight_engine.classify_epc()` — no fixtures needed.
- Integration tests for `parse_*_csv()` functions using `data/fixtures/*.csv` loaded via `conftest.py` fixtures.
- Async tests for `async_score_product` and `async_minimax_chat` use `pytest-asyncio` mode `auto`; mock `httpx.AsyncClient` via `respx` (added to dev deps as `respx==0.21.1`).
- Telegram handlers are **not** unit-tested (per brainstorming scope — keep simple); validated by manual `/start` test after deployment.

**Test commands:**
```
cd hermes-v1
pytest tests/ -v
pytest tests/test_epc.py::test_compute_zero_clicks -v   # smoke
```

**Coverage target:** ≥ 80% lines on `modules/`. No coverage gate enforced (kept simple).

**Edge cases explicitly tested:**
1. `compute(0, 5.0) == 0.0` — division-by-zero guard.
2. `compute(251, 6.34) == 0.025259...` — matches user's brainstorming example.
3. `_derive_campaign_key("FB----") == "FB"` — trailing dash strip.
4. `_derive_campaign_key("----") == "DIRECT"` — empty fallback.
5. `classify_epc(0.06) == "SCALE"`, `classify_epc(0.03) == "HOLD"`, `classify_epc(0.005) == "KILL"`.
6. Multi-item order grouping: fixture has one `Conversion id` with 3 items; verify only one click attribution counted.
7. Pending orders excluded from commission total.

[Implementation Order]
Follow the brainstorming build order (Telegram → MiniMax → Scoring → Meta → Shopee → EPC → Insights), with tests and fixtures interleaved so each layer is validated before the next is built.

1. **Scaffold project tree** [x] — Create `hermes-v1/` directory, `modules/`, `data/{uploads,exports,fixtures}`, `tests/`, `docs/`. Copy brainstorming spec into `docs/`. Create `requirements.txt`, `.env.example`, `.gitignore`, `README.md`.

2. **Define types and constants** [x] — Create `modules/models.py` (all dataclasses) and `modules/constants.py` (thresholds, allowed commands). Pure-Python, no external deps.

3. **Install dependencies** [x] — `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`. Verify `import telegram` and `import httpx` succeed.

4. **Build EPC calculator (pure)** [x] — `modules/epc_calculator.py` with `compute` + `compute_bulk`. Write `tests/test_epc.py`. **Run `pytest` → must pass.** Foundation every other module depends on.

5. **Build insight classifier (pure)** [x] — `modules/insight_engine.py` with `classify_epc`. Write `tests/test_insight.py`. **Run `pytest` → must pass.**

6. **Build Meta analyzer** [x] — `modules/meta_analyzer.py` with `parse_meta_csv`, `summarize_campaign`, `best_creative`, `worst_creative`. `data/fixtures/meta_sample.csv` covers VIDEO PURDAH / VIDEO GLOVE / VIDEO BANGKU BOX + 2 extra rows including a zero-click row. `tests/test_meta.py` has 38 tests. **81/81 green in 0.39s** (16 EPC + 31 Insight + 38 Meta + 0 Shopee wait — see Step 7). Source-of-truth refactor: `_META_HEADERS` tuple drives both the lowercase lookup map and the user-facing Bahasa error message.

7. **Build Shopee analyzer** [x] — `modules/shopee_analyzer.py` with `parse_click_csv`, `parse_commission_csv`, `aggregate_by_campaign`, `top_channel`, `_derive_campaign_key`. Fixtures: 25-row `shopee_click_sample.csv` (14 FB + 8 produkFB + 3 DIRECT) and 10-row `shopee_commission_sample.csv` (9 Completed + 1 Pending; multi-item order `260618D4CNWBBW` × 3 items). `tests/test_shopee.py` covers derivation rule, header normalization, type coercion, multi-item conversion counting, and EPC math against hand-computed fixtures. **140/140 green in 0.68s** (16 EPC + 31 Insight + 38 Meta + 59 Shopee). `_derive_campaign_key` strips whitespace *before* trailing dashes so stray spaces in real Sub_id cells don't leak into the campaign_key. Multi-item orders count as **1** conversion but contribute full commission sum (raw row count exposed as `completed_commission_rows` for sanity-checking).

8. **Build MiniMax wrapper** [x] — `minimax.py` at repo root (not in `modules/`) since the brainstorming tree puts it alongside `bot.py` / `prompts.py`. 4 functions: `async_minimax_chat` (public), `_build_payload` (pure, OpenAI-compatible body builder), `_handle_response` (status-code + schema validation), `get_client` (singleton `httpx.AsyncClient` with bearer-token auth from env). Plus `close_client` for graceful shutdown and `MiniMaxError` exception. Singleton client amortises TLS handshake across calls. Retry loop: 2 attempts (1 retry) with 1s/2s exponential backoff on transient transport errors and retryable HTTP statuses (`408, 425, 429, 500, 502, 503, 504`). 30s request timeout / 10s connect timeout. `.env` loaded at import time; missing `MINIMAX_API_KEY` raises `MiniMaxError` with actionable message. Smoke-tested: import succeeds, payload shape matches OpenAI spec (with/without system), `ValueError` on empty prompt / bad temperature / `max_tokens <= 0`. **No automated tests** per plan (requires live API); full pytest still 140/140 green.

9. **Build product scorer** [x] — `modules/product_scorer.py` with `async_score_product` + `_parse_scoring_response`. `prompts.py` with `PRODUCT_SCORING_PROMPT` + `HERMES_SYSTEM_PERSONA`. `tests/test_product_scorer.py` with 49 tests (38 pure parsing + 7 input validation + 4 async pipeline via `respx` mock). Handles 3 response shapes: bare JSON, ```` ```json ` fence, and prose-embedded JSON (with balanced-brace extractor). URL path is `/v1/chat/completions`. **189/189 green in 1.70s**, 0 warnings.


10. **Wire insight engine end-to-end** [x] — `modules/insight_engine.py` now exports `build_kpis`, `render_summary`, `classify_many`, `DEFAULT_CAMPAIGN_MAP` (in addition to the existing `classify_epc`). Three layers: (1) `classify_epc` (pure, single source of truth = `constants.EPC_*_THRESHOLD`), (2) `build_kpis` joins Meta + Shopee by `campaign_key` via `DEFAULT_CAMPAIGN_MAP = {"SIFAT": "FB", "PRODUK": "produkFB"}` (overridable via `campaign_map` kwarg; unmapped ad-sets keep their raw name so Meta spend is never silently dropped), (3) `render_summary` formats KPI list as Telegram-friendly plain text — SCALE first, then HOLD, then KILL — with 🟢/🟡/🔴 markers and `—` for missing Meta fields. `tests/test_insight_engine.py` adds 25 tests: empty/zero cases, default-map + custom-map + missing-map joins, weighted CTR/CPC math, Pending-order exclusion, multi-item commission sum, DIRECT preservation, classification threshold mapping, ordering + tier markers + footer singular/plural, and **end-to-end integration with real CSV fixtures** verifying FB commission total = 26.54 (matches brainstorming example). **214/214 green in 1.65s**, 0 warnings.

11. **Build Telegram bot** — `bot.py` with all 7 handlers + `SessionState`. Document upload flow saves files to `data/uploads/`. `/insights` command renders `insight_engine.render_summary()`.

12. **User provides real credentials** — User pastes `TELEGRAM_BOT_TOKEN` and `MINIMAX_API_KEY` into `hermes-v1/.env`. Verify with `python -c "from dotenv import load_dotenv; load_dotenv(); import os; print(os.getenv('TELEGRAM_BOT_TOKEN')[:10])"`.

13. **Smoke test live bot** — Run `python bot.py`. In Telegram: `/start` → `/help` → `/score test product | 10.00 | electronics` → upload Meta CSV → upload Shopee click CSV → upload Shopee commission CSV → `/insights`. Verify SCALE/HOLD/KILL output matches the real CSV data math (EPC for `FB` campaign = sum of Completed commission ÷ click count).

14. **Wire remaining prompts** — Add `META_ANALYSIS_PROMPT` to `prompts.py` (used by `analyze_meta_cmd` for qualitative summary on top of the numeric KPIs) and `INSIGHT_PROMPT` (used by `/insights` to generate the explanation paragraph).

15. **Final pytest run + manual checklist** — All 4 test files pass. Manual run of all 6 commands works. Logs to `hermes.log` are clean. `.env` is gitignored.

**Risk mitigation:**
- If MiniMax API contract changes during build, isolate impact to `minimax.py` and `product_scorer.py` — every other module is independent.
- If user's real CSVs reveal more columns we missed, update `models.py` first (Step 2) then propagate — types are the single source of truth.
- If Telegram handler tests prove flaky, skip them per brainstorming doc ("keep it simple") and rely on manual `/start` smoke test (Step 13).
