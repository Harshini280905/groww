"""AI narration layer — the ONLY place an LLM is allowed to touch this system.

Blueprint §11 (AI boundary rule): a model may explain a number, never decide
one. This module is called strictly AFTER a SignificantEventRow already
exists in the database (see routers/stocks.py::narrate) — the price, the
z-score, the tier all come from the deterministic pipeline in
market_data.py / significance.py. This module's only job is to turn
already-confirmed facts into a short, cited, human-readable sentence. It
cannot create an event, alter one, or influence what counts as significant.

Two-stage design, and the fallback is real, not a stub:
  1. Fetch real news headlines for the symbol via yfinance — free, no key,
     no LLM involved. This alone gives a genuine citation with zero config.
  2. IF ANTHROPIC_API_KEY is set, ask Claude to synthesize a short
     explanation from ONLY the confirmed stats + those headlines, with an
     explicit instruction to say "no clear cause found" rather than invent
     one. IF no key is set, skip synthesis and return the top headline
     directly — still cited, just not LLM-synthesized.

The response always states which path produced it (`generated_by`), so
nothing is ever presented as AI-generated when it wasn't. Never raises —
every failure path (missing key, network error, empty model response)
degrades to the headline fallback rather than crashing the caller.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

try:
    import yfinance as yf
except ImportError:                    # pragma: no cover - import guard
    yf = None

try:
    import anthropic
except ImportError:                    # pragma: no cover - import guard
    anthropic = None

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_NARRATOR_MODEL", "claude-haiku-4-5-20251001")

NARRATOR_SYSTEM_PROMPT = (
    "You explain an already-confirmed stock price move using ONLY the facts "
    "given to you. You are given: the exact price move, its statistical "
    "significance (z-score), and a list of real news headlines with links. "
    "Rules, strictly: "
    "(1) Never state a price, percentage, or number other than the ones "
    "given to you verbatim. "
    "(2) Only reference headlines from the provided list, by title. "
    "(3) If none of the headlines plausibly explain the move, say exactly "
    "that — do not invent a cause. "
    "(4) Never give investment advice or a buy/sell opinion. "
    "(5) Answer in 2-3 plain sentences, no markdown."
)


@dataclass(frozen=True)
class NewsItem:
    title: str
    publisher: str
    link: str
    published_at: Optional[str] = None


@dataclass(frozen=True)
class Narration:
    text: str
    generated_by: str          # "claude-api" | "headline-fallback" | "no-news-found"
    sources: list[NewsItem]
    model: Optional[str] = None
    error: Optional[str] = None


def fetch_recent_news(symbol: str, limit: int = 5) -> list[NewsItem]:
    """Real news headlines via yfinance — free, no key, no LLM involved.
    Never raises; returns an empty list on any failure. yfinance's news
    payload schema has shifted across library versions, so both the newer
    nested `content` shape and the older flat shape are handled."""
    if yf is None:
        return []
    try:
        yf_sym = symbol if "." in symbol else f"{symbol}.NS"
        raw = yf.Ticker(yf_sym).news or []
    except Exception:
        return []

    items: list[NewsItem] = []
    for entry in raw[:limit]:
        content = entry.get("content", entry) if isinstance(entry, dict) else {}
        title = content.get("title") or (entry.get("title") if isinstance(entry, dict) else None)
        if not title:
            continue
        publisher = (
            (content.get("provider") or {}).get("displayName")
            or entry.get("publisher")
            or "unknown source"
        )
        link = (
            (content.get("canonicalUrl") or {}).get("url")
            or (content.get("clickThroughUrl") or {}).get("url")
            or entry.get("link")
            or ""
        )
        published = content.get("pubDate") or entry.get("providerPublishTime")
        items.append(
            NewsItem(
                title=title, publisher=publisher, link=link,
                published_at=str(published) if published else None,
            )
        )
    return items


def _headline_fallback(news: list[NewsItem]) -> Narration:
    if not news:
        return Narration(
            text="No AI narration available and no recent news found for this symbol.",
            generated_by="no-news-found",
            sources=[],
        )
    top = news[0]
    return Narration(
        text=(
            f'Most recent related headline: "{top.title}" ({top.publisher}). '
            f"No ANTHROPIC_API_KEY is configured, so this is a direct headline "
            f"lookup, not an AI-synthesized explanation."
        ),
        generated_by="headline-fallback",
        sources=news[:3],
    )


def narrate_event(
    symbol: str,
    direction: str,
    return_pct: float,
    z_score: float,
    confidence: float,
    tier: str,
) -> Narration:
    """The only entry point. Must be called strictly after a
    SignificantEventRow is already persisted — see
    routers/stocks.py::narrate_event_endpoint. Synchronous / blocking
    (network calls to yfinance and optionally Anthropic) — callers on an
    async path should wrap this in `asyncio.to_thread`.
    """
    news = fetch_recent_news(symbol)

    if not ANTHROPIC_API_KEY or anthropic is None:
        return _headline_fallback(news)

    if not news:
        return Narration(
            text="No recent news found for this symbol; nothing to synthesize.",
            generated_by="no-news-found",
            sources=[],
        )

    headlines_block = "\n".join(f'- "{n.title}" ({n.publisher}) {n.link}' for n in news)
    user_prompt = (
        f"Symbol: {symbol}\n"
        f"Confirmed move: {direction} {return_pct:+.2f}%\n"
        f"Statistical significance: z={z_score:+.2f}\n"
        f"Data confidence tier: {tier}\n\n"
        f"Recent headlines:\n{headlines_block}\n\n"
        f"Explain this move using only the above."
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=220,
            system=NARRATOR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            raise ValueError("empty response from model")
        return Narration(
            text=text, generated_by="claude-api", sources=news[:3], model=ANTHROPIC_MODEL
        )
    except Exception as e:
        fallback = _headline_fallback(news)
        return Narration(
            text=fallback.text,
            generated_by=fallback.generated_by,
            sources=fallback.sources,
            error=f"{type(e).__name__}: {str(e)[:150]}",
        )
