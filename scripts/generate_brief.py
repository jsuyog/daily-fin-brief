#!/usr/bin/env python3
"""
Daily Fin Brief Generator
=========================
Fully automated pipeline:

1. Fetches end-of-day market context (can be extended with live APIs)
2. Calls GenAI (xAI Grok preferred, Claude as fallback) with a strict JSON schema
3. Produces a rich brief matching the site format:
   - Market overview, drivers, sectors
   - Top 5 Up / Top 5 Down stocks with specific reasoning
   - Impact on Tomorrow (AI assessment)
4. Saves into data/briefs.json (newest first)

Supported providers (set via env or GitHub Secrets):
  - XAI_API_KEY      → uses xAI Grok (recommended)
  - ANTHROPIC_API_KEY → uses Claude
  - LLM_PROVIDER     → force "xai" or "claude" (optional)

Usage:
  python scripts/generate_brief.py
  FORCE=1 python scripts/generate_brief.py   # overwrite today's brief
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Load local secrets (secrets.env) if present – never committed
# ---------------------------------------------------------------------------
def _load_secrets_env() -> None:
    """Load KEY=VALUE pairs from secrets.env into os.environ (does not override existing)."""
    secrets_path = Path(__file__).parent.parent / "secrets.env"
    if not secrets_path.exists():
        return
    with open(secrets_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value

_load_secrets_env()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
DATA_FILE = Path(__file__).parent.parent / "data" / "briefs.json"

XAI_API_URL = "https://api.x.ai/v1/chat/completions"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Models (adjust if you have access to newer ones)
XAI_MODEL = os.getenv("XAI_MODEL", "grok-3")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def get_day_name() -> str:
    return datetime.now(IST).strftime("%A")


def load_briefs() -> list:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_briefs(briefs: list) -> None:
    briefs.sort(key=lambda x: x["date"], reverse=True)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(briefs, f, indent=2, ensure_ascii=False)
    print(f"✅ Saved {len(briefs)} briefs → {DATA_FILE}")


def choose_provider() -> str:
    """Return 'xai' or 'claude' based on available keys / preference."""
    forced = os.getenv("LLM_PROVIDER", "").lower().strip()
    if forced in ("xai", "claude"):
        return forced

    has_xai = bool(os.getenv("XAI_API_KEY"))
    has_claude = bool(os.getenv("ANTHROPIC_API_KEY"))

    if has_xai:
        return "xai"
    if has_claude:
        return "claude"
    raise RuntimeError(
        "No API key found. Set XAI_API_KEY and/or ANTHROPIC_API_KEY "
        "as environment variables or GitHub Secrets."
    )


# ---------------------------------------------------------------------------
# Prompt – matches the exact schema the frontend expects
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a sharp Indian equity market analyst writing a daily closing brief.
Output ONLY valid JSON. No markdown, no commentary outside the JSON.

The JSON must follow this exact schema (all fields required):

{
  "date": "YYYY-MM-DD",
  "day": "Weekday",
  "status": "closed",
  "updated_at": "ISO timestamp with +05:30",
  "summary": {
    "nifty": {"value": number, "change": number, "change_pct": number, "prev_close": number, "open": number, "day_high": number, "day_low": number},
    "sensex": {"value": number, "change": number, "change_pct": number, "prev_close": number},
    "market_bias": "mildly negative | mildly positive | strongly positive | strongly negative | mixed",
    "top_performer": {"name": "string", "change_pct": number, "reason": "short reason"},
    "headline": "one crisp headline line"
  },
  "market_overview": "2-4 sentence paragraph of what happened today",
  "drivers": [
    {"tag": "SHORT TAG", "color": "blue|amber|red|green|gray", "text": "1-2 sentence explanation"}
  ],
  "sectors": [
    {"name": "IT|Auto|Pharma|Banks|Metals|FMCG|Realty|...", "status": "short status", "bias": "positive|negative|neutral"}
  ],
  "up_stocks": [
    {
      "name": "Stock Name",
      "ticker": "TICKER",
      "price_note": "price or relative note",
      "change_note": "+x% or Relative strength",
      "reasoning": "SPECIFIC why it went up today (not generic)",
      "news": "what the market wrap / news said",
      "trend_note": "short trend context",
      "chart": "https://finance.yahoo.com/quote/TICKER.NS",
      "chart_label": "Yahoo Finance · TICKER.NS"
    }
  ],
  "down_stocks": [ same structure as up_stocks ],
  "global_cues": "1-2 sentences on global markets / crude / geopolitics",
  "fii_dii": "FII/DII numbers or note if available",
  "outlook": ["point 1", "point 2", "point 3", "point 4", "point 5"],
  "impact_next_day": {
    "title": "Impact on Tomorrow / Next Trading Day (AI Assessment)",
    "summary": "2-3 sentence overall assessment of how today affects the next session",
    "key_points": ["point 1", "point 2", "point 3", "point 4", "point 5"],
    "bias_for_tomorrow": "short bias statement"
  },
  "previous_day_note": "one line about previous session if relevant"
}

Rules:
- Be specific in reasoning (mention actual drivers, not vague phrases).
- up_stocks and down_stocks must each have exactly 5 items.
- drivers: 4-6 items.
- outlook: 4-5 crisp actionable points.
- impact_next_day is mandatory and must be thoughtful.
- Use real Yahoo Finance chart URLs ending in .NS for Indian stocks.
- Output pure JSON only.
"""


def build_user_prompt(market_context: str, today: str, day: str) -> str:
    return f"""Today is {day}, {today} (Indian market close).

Here is the raw market context for today:

{market_context}

Generate the full daily fin brief JSON for this date.
Fill realistic numbers and specific stock reasoning based on the context above.
If some numbers are missing, use the most recent known closing levels and note the limitation briefly in market_overview.
"""


# ---------------------------------------------------------------------------
# GenAI callers
# ---------------------------------------------------------------------------
def call_xai(system: str, user: str) -> str:
    api_key = os.environ["XAI_API_KEY"]
    payload = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
        "max_tokens": 4096,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        XAI_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def call_claude(system: str, user: str) -> str:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "temperature": 0.4,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    # Claude returns content as a list of blocks
    parts = body.get("content", [])
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return text


def extract_json(text: str) -> dict:
    """Robustly extract JSON from model output (handles markdown fences)."""
    text = text.strip()
    # Remove ```json ... ``` if present
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    # Find first { ... last }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model response")
    return json.loads(text[start : end + 1])


def generate_with_genai(market_context: str) -> dict:
    provider = choose_provider()
    today = get_today_ist()
    day = get_day_name()
    user_prompt = build_user_prompt(market_context, today, day)

    print(f"🤖 Calling GenAI provider: {provider.upper()} ...")

    if provider == "xai":
        raw = call_xai(SYSTEM_PROMPT, user_prompt)
    else:
        raw = call_claude(SYSTEM_PROMPT, user_prompt)

    brief = extract_json(raw)

    # Light validation / defaults
    brief.setdefault("date", today)
    brief.setdefault("day", day)
    brief.setdefault("status", "closed")
    brief.setdefault("updated_at", datetime.now(IST).isoformat())

    if "up_stocks" not in brief or "down_stocks" not in brief:
        raise ValueError("Model response missing up_stocks / down_stocks")
    if "impact_next_day" not in brief:
        raise ValueError("Model response missing impact_next_day")

    print("✅ GenAI brief generated successfully")
    return brief


# ---------------------------------------------------------------------------
# Market context (placeholder – replace with real fetch later)
# ---------------------------------------------------------------------------
def fetch_market_context() -> str:
    """
    Returns a text block describing today's market.
    Later: replace with yfinance / NSE / scraping.
    For now we pass a structured summary so the LLM has something concrete.
    """
    # You can improve this by adding real API calls.
    # Example with yfinance (uncomment after adding the package):
    #
    # import yfinance as yf
    # nifty = yf.Ticker("^NSEI").history(period="2d")
    # ...

    today = get_today_ist()
    return f"""
Date: {today}
Market status: Closed (end of day)

Known reference levels (update these when you have live data):
- Previous session (3 Aug): Nifty 24,774 (+1.60%), Sensex 78,639 (+0.70%), IT led.
- Today (4 Aug) closing wrap consensus:
  - Nifty ~24,615 (−0.64%), Sensex ~78,429 (−0.27%), Bank Nifty ~−1.30%
  - Snapped 4-day winning streak
  - Caution ahead of RBI MPC outcome (expected next session)
  - Metals resilient; Banking, IT, FMCG saw profit-booking
  - Top relative strength: Hindalco, Trent, BEL, Bajaj Finance, Tata Steel
  - Under pressure: Grasim (profit booking after +5% Monday), HDFC Life, HUL, HDFC Bank, Reliance
  - LIC OFS overhang on insurance names
  - Second day of new Closing Auction Session (CAS) mechanism

Write a full institutional-quality closing brief with specific stock reasoning and a clear Impact on Tomorrow section focused on the RBI event.
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    today = get_today_ist()
    force = os.getenv("FORCE", "").lower() in ("1", "true", "yes")

    print(f"🚀 Daily Fin Brief Generator — {today}")
    print(f"   FORCE overwrite: {force}")

    briefs = load_briefs()

    if any(b["date"] == today for b in briefs) and not force:
        print(f"ℹ️  Brief for {today} already exists. Set FORCE=1 to overwrite.")
        print("   Exiting without changes.")
        return

    try:
        context = fetch_market_context()
        brief = generate_with_genai(context)
    except Exception as e:
        print(f"❌ Generation failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Replace if exists, otherwise insert at front
    briefs = [b for b in briefs if b["date"] != today]
    briefs.insert(0, brief)
    save_briefs(briefs)

    print("✅ Pipeline complete. Ready for git commit.")


if __name__ == "__main__":
    main()
