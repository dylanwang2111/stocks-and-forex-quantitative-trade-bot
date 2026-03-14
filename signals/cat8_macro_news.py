"""
signals/cat8_macro_news.py
Category 8: Macro / News Sentiment
LLM analysis of recent headlines via Yahoo Finance RSS feed.

LLM priority:
  1. Gemini Flash 2.0 — if GEMINI_API_KEY is set.
  2. Groq Llama 3.3 70B — elif GROQ_API_KEY is set (free tier).
  3. Disabled — vote=0 if neither key is configured.

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
_CACHE_TTL = 900  # 15 minutes

_DISABLED_RESULT = SignalResult(0, "LLM disabled — set GEMINI_API_KEY or GROQ_API_KEY", {})
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

Also classify the MACRO RISK LEVEL:
- HIGH: active war/conflict, imminent FOMC, major geopolitical shock
- MEDIUM: trade tensions, political uncertainty, mixed macro signals
- LOW: calm markets, no major macro event, clear directional news

Respond with a JSON object ONLY (no markdown):
{{"sentiment": "BULLISH"|"BEARISH"|"NEUTRAL", "confidence": 0-100, "risk_level": "HIGH"|"MEDIUM"|"LOW", "reason": "one sentence"}}
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

    # ── Fetch headlines from Yahoo Finance RSS (shared by all LLM backends) ────
    headlines: list[str] = []
    try:
        headlines = _fetch_yf_rss(symbol)
    except Exception:
        pass  # fall through to macro-only LLM call

    # ── 1. Try Gemini ──────────────────────────────────────────────────────────
    if settings.gemini.enabled:
        try:
            if headlines:
                return _analyse_with_gemini(symbol, headlines, settings)
            else:
                return _macro_only_gemini(symbol, settings)
        except Exception as e:
            return SignalResult(0, f"Gemini analysis failed: {e}", {"error": str(e)})

    # ── 2. Try Groq ────────────────────────────────────────────────────────────
    if settings.groq.enabled:
        try:
            if headlines:
                return _analyse_with_groq(symbol, headlines, settings)
            else:
                return _macro_only_groq(symbol, settings)
        except Exception as e:
            return SignalResult(0, f"Groq analysis failed: {e}", {"error": str(e)})

    # ── 3. Neither key configured ──────────────────────────────────────────────
    return _DISABLED_RESULT


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
    from google import genai

    client = genai.Client(api_key=settings.gemini.api_key)
    prompt = (
        f"In one sentence, what is the current macro market sentiment for {symbol}? "
        f'Respond with JSON only: {{"sentiment": "BULLISH"|"BEARISH"|"NEUTRAL", '
        f'"confidence": 0-100, "risk_level": "HIGH"|"MEDIUM"|"LOW", "reason": "..."}}'
    )
    response = client.models.generate_content(model=settings.gemini.model, contents=prompt)
    return _parse_gemini_response(response.text, symbol, source="macro_only")


def _analyse_with_gemini(symbol: str, headlines: list[str], settings: Any) -> SignalResult:
    from google import genai

    client = genai.Client(api_key=settings.gemini.api_key)
    headline_text = "\n".join(f"- {h}" for h in headlines)
    prompt = _GEMINI_PROMPT.format(symbol=symbol, headlines=headline_text)
    response = client.models.generate_content(model=settings.gemini.model, contents=prompt)
    return _parse_gemini_response(response.text, symbol, source="yahoo_rss", headlines=headlines)


def _macro_only_groq(symbol: str, settings: Any) -> SignalResult:
    """Ask Groq/Llama for macro context when no headlines are available."""
    try:
        from groq import Groq
    except ImportError:
        return SignalResult(0, "Groq package not installed — run: pip install groq", {})

    client = Groq(api_key=settings.groq.api_key)
    prompt = (
        f"In one sentence, what is the current macro market sentiment for {symbol}? "
        f'Respond with JSON only: {{"sentiment": "BULLISH"|"BEARISH"|"NEUTRAL", '
        f'"confidence": 0-100, "risk_level": "HIGH"|"MEDIUM"|"LOW", "reason": "..."}}'
    )
    completion = client.chat.completions.create(
        model=settings.groq.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=200,
    )
    text = completion.choices[0].message.content
    return _parse_gemini_response(text, symbol, source="macro_only")


def _analyse_with_groq(symbol: str, headlines: list[str], settings: Any) -> SignalResult:
    """Analyse headlines with Groq/Llama 3.3 70B."""
    try:
        from groq import Groq
    except ImportError:
        return SignalResult(0, "Groq package not installed — run: pip install groq", {})

    client = Groq(api_key=settings.groq.api_key)
    headline_text = "\n".join(f"- {h}" for h in headlines)
    prompt = _GEMINI_PROMPT.format(symbol=symbol, headlines=headline_text)

    completion = client.chat.completions.create(
        model=settings.groq.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=200,
    )
    text = completion.choices[0].message.content
    return _parse_gemini_response(text, symbol, source="yahoo_rss", headlines=headlines)


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

    # Validate risk_level with whitelist; default to "LOW" on unknown values
    raw_risk = str(parsed.get("risk_level", "LOW")).upper()
    risk_level = raw_risk if raw_risk in ("HIGH", "MEDIUM", "LOW") else "LOW"

    params = {
        "sentiment": sentiment,
        "confidence": confidence,
        "risk_level": risk_level,
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
        "USDJPY": "JPY=X",
        "AUDUSD": "AUDUSD=X",
        "USDCAD": "CAD=X",
        "USDCHF": "CHF=X",
        "BTCUSD": "BTC-USD",
        "ETHUSD": "ETH-USD",
    }
    return mapping.get(symbol, symbol)


def clear_cache() -> None:
    _CACHE.clear()
