/deep-planning 

# README.md

# Hermes AI - Affiliate Traffic Intelligence System

## Overview

Hermes is a Telegram-based AI assistant designed for Shopee Affiliate marketers running Meta Ads.

Hermes focuses on:

* Product clickability analysis
* Facebook and Threads hook generation
* Meta Ads performance analysis
* Shopee Affiliate click analysis
* EPC (Earning Per Click) analysis

Hermes is NOT a product winner prediction engine.

Hermes helps answer:

* Will this product generate clicks?
* Is this traffic valuable?
* Which campaign generates better EPC?
* Which traffic source should be scaled?

---

# Core Workflow

1. User finds a product.

2. User sends product information to Hermes.

3. Hermes generates:

   * Clickability score
   * Facebook hooks
   * Threads hooks
   * Tracking recommendation

4. User runs Meta Ads.

5. User exports:

   * Meta Ads report
   * Shopee Click report
   * Shopee Commission report

6. Hermes analyzes:

   * CTR
   * CPC
   * Click volume
   * Commission
   * EPC

7. Hermes identifies:

   * Best traffic generators
   * Best EPC campaigns
   * Scale / Hold / Kill suggestions

---

# Phase 1 Modules

## Module 1

Product Scoring

Command:

* /score

Output:

* Score
* Facebook hooks
* Threads content
* Clickability reasoning

---

## Module 2

Meta Ads Analyzer

Input:

* Meta Ads CSV

Output:

* CTR
* CPC
* Click ranking
* Best performing creatives

---

## Module 3

Shopee Analyzer

Input:

* Click Report CSV
* Commission Report CSV

Output:

* Total clicks
* Total commission
* EPC

Formula:

EPC = Commission / Clicks

---

## Module 4

Insights

Output:

* Best traffic source
* Best EPC source
* Scale recommendation
* Hold recommendation
* Kill recommendation

---

# KPI Priority

Priority 1:

* EPC

Priority 2:

* Link Clicks

Priority 3:

* CPC

Priority 4:

* CTR

Priority 5:

* Commission

---

# Success Criteria

Hermes can answer:

"Is this campaign generating valuable traffic?"

instead of

"Did this product directly convert?"


# AGENTS.md

You are Hermes AI.

Role:
Affiliate Traffic Intelligence Engine.

Purpose:
Help optimize Shopee Affiliate traffic acquisition using Meta Ads.

You must prioritize:

1. EPC (Earning Per Click)
2. Link Clicks
3. CPC Efficiency
4. CTR
5. Commission

You do NOT prioritize:

* Product conversion rate
* Product popularity
* Product reviews
* Product ratings

Reason:

Affiliate revenue may come from cookie attribution rather than direct product purchases.

---

When evaluating a product:

Evaluate:

* Curiosity
* Scroll stopping ability
* Mass appeal
* Click motivation

Do not evaluate:

* Product quality
* Product durability
* Long-term customer satisfaction

---

When analyzing campaigns:

Always calculate:

EPC = Commission / Clicks

Then classify:

* Scale
* Hold
* Kill

---

Output Style:

* Structured
* Short
* Analytical

Avoid:

* Motivational language
* Generic marketing advice
* Unverified assumptions

Think like a traffic analyst, not a salesperson.


# Development Rules

Phase 1 Scope Only

Allowed:

* Telegram Bot
* MiniMax Integration
* Product Scoring
* Meta Ads Analyzer
* Shopee Click Analyzer
* Shopee Commission Analyzer
* EPC Calculator
* Insight Summary

Not Allowed:

* Database
* Machine Learning
* Pattern Learning Engine
* Recommendation Engine
* Auto Campaign Builder
* Multi-Agent Architecture

File Structure:

hermes-v1/

* bot.py
* minimax.py
* prompts.py
* product_scorer.py
* meta_analyzer.py
* shopee_analyzer.py
* epc_calculator.py
* insight_engine.py
* .env

Build Order:

1. Telegram Bot
2. MiniMax Integration
3. Product Scoring
4. Meta Analyzer
5. Shopee Analyzer
6. EPC Calculator
7. Insight Engine

Rule:

Do not build Phase 2 features until Phase 1 is working with real reports.


hermes-v1/
│
├── bot.py
├── minimax.py
├── prompts.py
│
├── modules/
│   ├── product_scorer.py
│   ├── meta_analyzer.py
│   ├── shopee_analyzer.py
│   ├── epc_calculator.py
│   └── insight_engine.py
│
├── data/
│   ├── uploads/
│   └── exports/
│
├── docs/
│   ├── README.md
│   ├── AGENTS.md
│   └── DEVELOPMENT_RULES.md
│
├── tests/
│   ├── test_meta.py
│   ├── test_shopee.py
│   └── test_epc.py
│
├── .env
├── requirements.txt
└── .gitignore

What Each File Does
bot.py

Telegram entry point.

Responsibilities:

/start
/help
/score
Excel uploads

Nothing else.

minimax.py

Handles MiniMax API.

Responsibilities:

Send prompts
Receive responses
Error handling

Nothing related to Telegram.

prompts.py

Stores all prompts.

Example:

PRODUCT_SCORING_PROMPT
META_ANALYSIS_PROMPT
INSIGHT_PROMPT
📦 modules/

This is where all business logic lives.

product_scorer.py

Input:

Title
Price
Category

Output:

Click Potential Score
FB Hooks
Threads Hooks
meta_analyzer.py

Reads Meta export.

Calculates:

CTR
CPC
Link Clicks

Returns:

Best Creative
Worst Creative
shopee_analyzer.py

Reads:

Click Report
Commission Report

Calculates:

Total Clicks
Total Commission

Returns summary.

epc_calculator.py

Formula:

EPC = commission / clicks

Example:

251 clicks
RM6.34 commission

EPC = RM0.025
insight_engine.py

Combines:

Meta
Shopee
EPC

Outputs:

SCALE
HOLD
KILL
📂 data/

Temporary storage.

data/
├── uploads/
└── exports/
uploads/

User uploads:

meta_report.xlsx
click_report.xlsx
commission_report.xlsx
exports/

Future reports generated by Hermes.

Not needed immediately.

📂 docs/

Documentation only.

README.md

Project overview.

AGENTS.md

Hermes behavior rules.

DEVELOPMENT_RULES.md

Scope control.

This prevents Cursor from building Phase 2 features.

🧪 tests/

Keep it simple.

tests/
├── test_meta.py
├── test_shopee.py
└── test_epc.py

Purpose:

Verify calculations don't break.

🎯 Telegram Commands (Phase 1)

Only these:

/start
/help
/score
/analyze_meta
/analyze_shopee
/insights


Telegram
   │
   ▼
bot.py
   │
   ├── Product Scoring
   │       ▼
   │   MiniMax
   │
   ├── Meta Analyzer
   │
   ├── Shopee Analyzer
   │
   └── EPC Calculator
            │
            ▼
      Insight Engine
            │
            ▼
      SCALE / HOLD / KILL