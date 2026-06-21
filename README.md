# Hermes AI — Affiliate Traffic Intelligence

Phase-1 Telegram bot that ingests Meta Ads CSVs and Shopee Affiliate click/commission CSVs, computes EPC and traffic-quality KPIs, and emits structured SCALE / HOLD / KILL insights.

> **Scope:** Phase 1 only. No database, no ML, no multi-agent. See `docs/DEVELOPMENT_RULES.md`.

## Quickstart

### 1. Prerequisites
- Python 3.13+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A MiniMax API key

### 2. Setup
```bash
cd hermes-v1
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and paste your real TELEGRAM_BOT_TOKEN and MINIMAX_API_KEY
```

### 3. Run
```bash
python bot.py
```

### 4. Test
```bash
pytest tests/ -v
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Greeting + readiness |
| `/help` | List all commands |
| `/score <title> \| <price> \| <category>` | Score a product for clickability |
| `/analyze_meta` | Then upload Meta Ads CSV |
| `/analyze_shopee` | Then upload Shopee Click + Commission CSVs |
| `/insights` | Run the full traffic analysis |

## Workflow

1. Run a Meta Ads campaign (CBO recommended).
2. Export: Meta Ads CSV, Shopee Click Report CSV, Shopee Commission Report CSV.
3. Send `/analyze_meta` → upload Meta CSV.
4. Send `/analyze_shopee` → upload Click CSV, then Commission CSV.
5. Send `/insights` → receive SCALE / HOLD / KILL verdict per campaign.

## KPI Priority

1. **EPC** (Earnings Per Click) — `commission / clicks`
2. Link Clicks
3. CPC Efficiency
4. CTR
5. Commission

## Project Structure

```
hermes-v1/
├── bot.py                  # Telegram entry point
├── minimax.py              # MiniMax API wrapper
├── prompts.py              # All prompt templates
├── modules/
│   ├── models.py           # Dataclasses
│   ├── constants.py        # Thresholds
│   ├── product_scorer.py   # /score command
│   ├── meta_analyzer.py    # Meta CSV parser
│   ├── shopee_analyzer.py  # Shopee CSV parser
│   ├── epc_calculator.py   # Pure EPC math
│   └── insight_engine.py   # SCALE/HOLD/KILL logic
├── data/
│   ├── uploads/            # User-uploaded CSVs
│   ├── exports/            # Future report exports
│   └── fixtures/           # Test data
├── docs/
│   ├── README.md           # Brainstorming spec
│   ├── AGENTS.md           # Behavior rules
│   └── DEVELOPMENT_RULES.md # Phase-1 guardrails
├── tests/
│   ├── test_meta.py
│   ├── test_shopee.py
│   ├── test_epc.py
│   └── test_insight.py
├── requirements.txt
├── .env.example
└── README.md
```

## Logging

Runtime logs go to `hermes.log` (gitignored). Set `LOG_LEVEL=DEBUG` in `.env` for verbose output.

## License

Private — internal use only.
