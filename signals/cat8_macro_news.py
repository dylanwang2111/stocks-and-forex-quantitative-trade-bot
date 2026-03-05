"""
signals/cat8_macro_news.py
Category 8: Macro / News Sentiment
Gemini Flash 2.0 analysis of recent headlines via Yahoo Finance RSS feed.
Gracefully stubs if GEMINI_API_KEY is not set.
Rate-limited: cached 60 min per instrument.

News source: Yahoo Finance RSS (free, no API key required).
IBKR does not provide a news API for algorithmic use.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from signals import SignalResult

# ── Cache (60 min TTL per symbol) ──────────────────────────────────────────────
_CACHE: dict[str, tuple[float, SignalResult]] = {}
_CACHE_TTL = 3600  # 60 minutes

_DISABLED_RESULT = SignalResult(0, "LLM disabled — GEMINI_API_KEY not set", {})
_NO_HEADLINES    = SignalResult(0, "No recent headlines available", {})

# Yahoo Finance RSS — free, no key required
_YF_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"

_GEMINI_PROMPT = """You are a financial sentiment analyst.
Below are recent news headlines for the asset: {symbol}

Headlines:
{headlines}

Based ONLY on these headlines, classify the SHORT-TERM (next 4–8 hours) market sentiment as:
- BULLISH: headlines suggest upward price pressure
- BEARISH: headlines suggest downward price pressure
- NEUTRAL: mixed, unclear, or irrelevant headlines

Respond with a JSON object ONLY (no markdown):
{{"sentiment": "BULLISH"|"BEARISH"|"NEUTRAL", "confidence": 0-100, "reason": "one sentence"}}
"""


def evaluate(symbol: str) -> SignalResult:
    """
    Fetch recent news headlines for symbol via Yahoo Finance RSS,
    analyse with Gemini Flash 2.0.

    Graceful stubs:
    - GEMINI_API_KEY missing → vote=0, reason="LLM disabled"
    - RSS fetch fails        → Gemini asked for macro context only
    - Any exception          → vote=0, reason=error message
    """
    if symbol in _CACHE:
        cached_at, cached_result = _CACHE[symbol]
        if time.time() - cached_at < _CACHE_TTL:
            return cached_result

    result = _evaluate_internal(symbol)
    _CACHE[symbol] = (time.time(), result)
    return result


def _evaluate_internal(symbol: str) -> SignalResult:
    from config.settings import settings

    if not settings.gemini.enabled:
        return _DISABLED_RESULT

    # ── Fetch headlines from Yahoo Finance RSS ─────────────────────────────────
    headlines: list[str] = []
    try:
        headlines = _fetch_yf_rss(symbol)
    except Exception:
        pass  # fall through to macro-only Gemini call

    # ── Analyse with Gemini ────────────────────────────────────────────────────
    try:
        if headlines:
            return _analyse_with_gemini(symbol, headlines, settings)
        else:
            return _macro_only_gemini(symbol, settings)
    except Exception as e:
        return SignalResult(0, f"Gemini analysis failed: {e}", {"error": str(e)})


def _fetch_yf_rss(symbol: str) -> list[str]:
    """Fetch up to 10 headlines from Yahoo Finance RSS. No API key needed."""
    rss_symbol = _rss_symbol(symbol)
    url = _YF_RSS.format(symbol=rss_symbol)
    resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    titles = [item.findtext("title", "") for item in root.iter("item")]
    return [t for t in titles if t][:10]


def _macro_only_gemini(symbol: str, settings: Any) -> SignalResult:
    """Ask Gemini for macro context when no headlines are available."""
    import google.generativeai as genai

    genai.configure(api_key=settings.gemini.api_key)
    model = genai.GenerativeModel(settings.gemini.model)
    prompt = (
        f"In one sentence, what is the current macro market sentiment for {symbol}? "
        f'Respond with JSON only: {{"sentiment": "BULLISH"|"BEARISH"|"NEUTRAL", '
        f'"confidence": 0-100, "reason": "..."}}'
    )
    response = model.generate_content(prompt)
    return _parse_gemini_response(response.text, symbol, source="macro_only")


def _analyse_with_gemini(symbol: str, headlines: list[str], settings: Any) -> SignalResult:
    import google.generativeai as genai

    genai.configure(api_key=settings.gemini.api_key)
    model = genai.GenerativeModel(settings.gemini.model)

    headline_text = "\n".join(f"- {h}" for h in headlines)
    prompt = _GEMINI_PROMPT.format(symbol=symbol, headlines=headline_text)

    response = model.generate_content(prompt)
    return _parse_gemini_response(response.text, symbol, source="yahoo_rss", headlines=headlines)


def _parse_gemini_response(
    text: str,
    symbol: str,
    source: str = "unknown",
    headlines: list[str] | None = None,
) -> SignalResult:
    import json
    import re

    clean = re.sub(r"```(?:json)?", "", text).strip()

    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return SignalResult(0, f"Could not parse Gemini response: {text[:100]}")
        else:
            return SignalResult(0, f"No JSON in Gemini response: {text[:100]}")

    sentiment  = parsed.get("sentiment", "NEUTRAL").upper()
    confidence = int(parsed.get("confidence", 50))
    reason     = parsed.get("reason", "")

    params = {
        "sentiment": sentiment,
        "confidence": confidence,
        "source": source,
        "headlines_count": len(headlines) if headlines else 0,
    }

    if sentiment == "BULLISH" and confidence >= 60:
        return SignalResult(+1, f"Gemini: {reason}", params)
    if sentiment == "BEARISH" and confidence >= 60:
        return SignalResult(-1, f"Gemini: {reason}", params)

    return SignalResult(0, f"Gemini: neutral/low-confidence — {reason}", params)


def _rss_symbol(symbol: str) -> str:
    """Map bot symbols to Yahoo Finance RSS ticker format."""
    mapping = {
        "EURUSD": "EURUSD=X",
        "GBPUSD": "GBPUSD=X",
    }
    return mapping.get(symbol, symbol)


def clear_cache() -> None:
    _CACHE.clear()
