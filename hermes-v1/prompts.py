"""
Prompt templates for MiniMax.

Hermes drives three LLM-driven features in Phase 1:

  1. ``/score``        — product clickability scoring (PRODUCT_SCORING_PROMPT)
  2. ``/analyze_meta`` — qualitative Meta Ads summary  (META_ANALYSIS_PROMPT, Step 14)
  3. ``/insights``     — traffic / EPC narrative         (INSIGHT_PROMPT, Step 14)

All prompts share the AGENTS.md persona: Affiliate Traffic Intelligence
Engine that prioritises EPC > Link Clicks > CPC > CTR > Commission and
**never** evaluates product quality, durability, reviews, or ratings.

Rules enforced by every prompt:

* Output must be strict JSON, no markdown fences, no surrounding prose.
* Reasoning is short and analytical — no motivational language.
* Hooks are bounded (3 per platform, ≤ 90 chars each) so the bot can
  render them directly in Telegram without further trimming.
"""

from __future__ import annotations


__all__ = [
    "HERMES_SYSTEM_PERSONA",
    "PRODUCT_SCORING_PROMPT",
    "META_ANALYSIS_PROMPT",
    "INSIGHT_PROMPT",
]


# ---------------------------------------------------------------------------
# Shared system persona — prepended to every MiniMax call
# ---------------------------------------------------------------------------

HERMES_SYSTEM_PERSONA: str = (
    "You are Hermes, an Affiliate Traffic Intelligence Engine for "
    "Shopee Affiliate marketers running Meta Ads.\n"
    "\n"
    "KPI priority (always): 1) EPC (Earning Per Click), "
    "2) Link Clicks, 3) CPC efficiency, 4) CTR, 5) Commission.\n"
    "\n"
    "Evaluate only: curiosity, scroll-stopping visual potential, "
    "mass appeal, click motivation, price-point psychology.\n"
    "Do NOT evaluate: product quality, durability, reviews, ratings, "
    "or popularity. Affiliate revenue comes from cookie attribution, "
    "not direct purchase — a great traffic generator can convert poorly.\n"
    "\n"
    "Output style: structured, short, analytical. No motivational "
    "language. No generic marketing advice. No unverified assumptions. "
    "Think like a traffic analyst, not a salesperson.\n"
    "\n"
    "Hard rules for every response:\n"
    "  - Return ONLY a single JSON object. No markdown fences, no prose "
    "before or after.\n"
    "  - All string fields must be plain UTF-8 text — no emoji unless "
    "the schema explicitly allows it.\n"
    "  - Honour the exact field names and types requested in the user "
    "message; never invent extra fields.\n"
)


# ---------------------------------------------------------------------------
# /score — product clickability scoring (Step 9)
# ---------------------------------------------------------------------------

PRODUCT_SCORING_PROMPT: str = """\
Score the clickability of the Shopee affiliate product below on a \
0-100 scale and produce 3 Facebook hooks and 3 Threads hooks to \
drive traffic to it.

## Scoring rubric
- 0-30  : Low scroll-stop potential, narrow appeal, weak click \
motivation. Skip.
- 31-55 : Average. Some curiosity, but blends into the feed.
- 56-75 : Strong. Clear scroll-stop signal + click motivation \
(price shock, novelty, problem-solver, FOMO).
- 76-100: Top-decile. Pattern-interrupt visual or contrarian \
framing likely; mass-market appeal.

## What drives the score UP
- Curiosity gap (something unusual / unexplained).
- Visual or copy pattern-interrupt potential.
- Mass-market appeal (typical MY/ID/PH Shopee buyer would click).
- Strong click motivation (urgency, price shock, problem solver, FOMO).
- Price-point psychology (anchoring to a familiar reference price).

## What drives the score DOWN
- Bland or generic product category.
- Niche audience only.
- Weak visual hook (hard to express in a single frame).
- No click motivation — looks like a search-driven purchase, not an \
impulse click.

## Hook rules
- Exactly 3 hooks per platform. No more, no fewer.
- Each hook <= 90 characters.
- Facebook hooks: pain-point or curiosity openers. Conversational, \
slightly punchy. May use Bahasa Malaysia or English.
- Threads hooks: shorter, conversational, drop-knowledge or \
contrarian framing. May use Bahasa Malaysia or English.
- BANNED phrases: "Buy now", "Limited time", "Don't miss out", \
"Click here", "Order now", generic sales CTAs.

## Reasoning rules
- 2-3 sentences max.
- Cite which signals (curiosity, scroll-stop, mass appeal, click \
motivation) drove the score up or down.
- Reference the price-point when relevant.

## Product input
Title: {title}
Price (RM): {price}
Category: {category}

## Required JSON output (strict, no markdown fences)
{{
  "score": <integer 0-100>,
  "fb_hooks": ["<hook 1>", "<hook 2>", "<hook 3>"],
  "threads_hooks": ["<hook 1>", "<hook 2>", "<hook 3>"],
  "reasoning": "<2-3 sentence justification>"
}}
"""


# ---------------------------------------------------------------------------
# Stubs — filled in Step 14 (Wire remaining prompts)
# ---------------------------------------------------------------------------
#
# These are intentionally empty for now; Step 14 will replace them with
# the full qualitative-summary and insight-narrative prompts used by
# /analyze_meta and /insights respectively. Keeping them declared here
# means downstream modules can import the names without crashing.

META_ANALYSIS_PROMPT: str = ""  # Step 14
INSIGHT_PROMPT: str = ""        # Step 14
