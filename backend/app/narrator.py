"""AI narration layer — the ONLY place an LLM is allowed to touch this system.

Blueprint §11 (AI boundary rule): a model may explain a number, never decide
one. This module is called strictly AFTER a SignificantEventRow already
exists in the database (see routers/stocks.py::narrate_event_endpoint) —
the price, the z-score, the tier all come from the deterministic pipeline
in market_data.py / significance.py. This module's only job is to turn
already-confirmed facts into a short, cited, human-readable sentence. It
cannot create an event, alter one, or influence what counts as significant.

PROVIDER-AGNOSTIC BY DESIGN. Three paths, resolved at call time:

  * Groq (or any OpenAI-compatible endpoint) — set GROQ_API_KEY. Groq's
    free tier needs no credit card, which makes it the practical default
    for a hackathon. Because the wire format is OpenAI-compatible, pointing
    GROQ_BASE_URL at OpenRouter / Together / a local Ollama works through
    this exact same code path with no code change.
  * Anthropic — set ANTHROPIC_API_KEY, uses the native SDK.
  * Neither — falls back to a real news headline lookup, cited, and says
    so plainly.

Stage 1 (news fetch via yfinance) runs regardless of provider: free, no key,
no LLM. That alone yields a genuine citation with zero configuration.

Every response states which path produced it (`generated_by`), so nothing is
ever presented as AI-generated when it wasn't. Never raises — every failure
(missing key, network error, empty response, bad model name) degrades to the
headline fallback rather than crashing the caller.
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
    import httpx
except ImportError:                    # pragma: no cover - import guard
    httpx = None

try:
    import anthropic
except ImportError:                    # pragma: no cover - import guard
    anthropic = None


# "auto" (default) picks Groq if its key is present, else Anthropic, else
# the headline fallback. Set explicitly to pin one provider.
NARRATOR_PROVIDER = os.getenv("NARRATOR_PROVIDER", "auto").lower()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
# Groq rotates its lineup and retires models without much notice —
# llama-3.3-70b-versatile was the default here and now 404s. If narration
# starts failing with a 404, list what your key can actually reach:
#   curl -H "Authorization: Bearer $GROQ_API_KEY" \
#        https://api.groq.com/openai/v1/models
# and set GROQ_NARRATOR_MODEL accordingly.
GROQ_MODEL = os.getenv("GROQ_NARRATOR_MODEL", "openai/gpt-oss-120b")

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
    # "groq-api" | "anthropic-api" | "headline-fallback" | "no-news-found"
    generated_by: str
    sources: list[NewsItem]
    model: Optional[str] = None
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — real news, no LLM, no key required
# ─────────────────────────────────────────────────────────────────────────────

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


def _headline_fallback(news: list[NewsItem], reason: str = "no LLM provider configured") -> Narration:
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
            f"This is a direct headline lookup, not an AI-synthesized "
            f"explanation ({reason})."
        ),
        generated_by="headline-fallback",
        # Return the FULL list, not a slice — see the note in narrate_event:
        # the caller must be able to verify every headline the narration
        # could possibly reference.
        sources=news,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — optional LLM synthesis
# ─────────────────────────────────────────────────────────────────────────────

def _build_user_prompt(
    symbol: str, direction: str, return_pct: float, z_score: Optional[float], tier: str,
    news: list[NewsItem],
) -> str:
    headlines_block = "\n".join(f'- "{n.title}" ({n.publisher}) {n.link}' for n in news)
    # z_score is None when explaining an ordinary day's move rather than a
    # confirmed significant event. Emitting "z=+0.00" there would invite the
    # model to describe a normal move as statistically notable — so the line
    # is omitted entirely, and the model is told the move is within range.
    if z_score is None:
        significance_line = (
            "Statistical significance: none — this is a routine daily move, "
            "NOT flagged as unusual. Do not describe it as significant, "
            "dramatic, or a spike.\n"
        )
    else:
        significance_line = f"Statistical significance: z={z_score:+.2f}\n"
    return (
        f"Symbol: {symbol}\n"
        f"Confirmed move: {direction} {return_pct:+.2f}%\n"
        f"{significance_line}"
        f"Data confidence tier: {tier}\n\n"
        f"Recent headlines:\n{headlines_block}\n\n"
        f"Explain this move using only the above."
    )


def _synthesize_openai_compatible(
    api_key: str, base_url: str, model: str, system_prompt: str, user_prompt: str
) -> str:
    """One code path for Groq and every other OpenAI-compatible endpoint.
    Raises on failure; the caller converts that into a graceful fallback."""
    if httpx is None:
        raise RuntimeError(
            "httpx is not installed — the Groq/OpenAI-compatible transport "
            "needs it. Run: pip install -r requirements.txt"
        )
    resp = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 700,
            "temperature": 0.3,      # low — this is factual summarisation
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


def _synthesize_anthropic(model: str, system_prompt: str, user_prompt: str) -> str:
    if anthropic is None:
        raise RuntimeError("anthropic SDK not installed")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=model,
        max_tokens=700,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    ).strip()


def resolve_provider() -> str:
    """Which provider will actually be used. Exposed so /api/health and the
    docs can report the truth rather than the user guessing."""
    explicit = (NARRATOR_PROVIDER or "auto").lower()
    if explicit in ("groq", "anthropic", "none"):
        return explicit
    if GROQ_API_KEY:
        return "groq"
    if ANTHROPIC_API_KEY and anthropic is not None:
        return "anthropic"
    return "none"


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def narrate_event(
    symbol: str,
    direction: str,
    return_pct: float,
    z_score: Optional[float],
    confidence: float,
    tier: str,
) -> Narration:
    """The only entry point. Must be called strictly after a
    SignificantEventRow is already persisted. Synchronous / blocking
    (network calls) — async callers should wrap in `asyncio.to_thread`.
    """
    news = fetch_recent_news(symbol)
    provider = resolve_provider()

    if provider == "none":
        return _headline_fallback(news)

    if not news:
        return Narration(
            text="No recent news found for this symbol; nothing to synthesize.",
            generated_by="no-news-found",
            sources=[],
        )

    user_prompt = _build_user_prompt(symbol, direction, return_pct, z_score, tier, news)

    try:
        if provider == "groq":
            if not GROQ_API_KEY:
                raise RuntimeError("NARRATOR_PROVIDER=groq but GROQ_API_KEY is unset")
            text = _synthesize_openai_compatible(
                GROQ_API_KEY, GROQ_BASE_URL, GROQ_MODEL,
                NARRATOR_SYSTEM_PROMPT, user_prompt,
            )
            model_used, tag = GROQ_MODEL, "groq-api"
        else:
            if not ANTHROPIC_API_KEY:
                raise RuntimeError("NARRATOR_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset")
            text = _synthesize_anthropic(
                ANTHROPIC_MODEL, NARRATOR_SYSTEM_PROMPT, user_prompt
            )
            model_used, tag = ANTHROPIC_MODEL, "anthropic-api"

        # Strip defensively rather than trusting the transport to have done
        # it — a whitespace-only reply is an empty reply, and a future
        # transport added here might not normalise on the way out.
        text = (text or "").strip()
        if not text:
            raise ValueError("empty response from model")

        # CITATION INTEGRITY: return exactly the headline set the model was
        # shown — never a subset. Returning news[:3] while prompting with 5
        # meant a legitimate citation of headline #4 looked to the user like
        # a fabrication, with no way to verify it. The whole "always cited"
        # guarantee depends on the reader being able to check every source
        # the narration could possibly reference.
        return Narration(text=text, generated_by=tag, sources=news, model=model_used)

    except Exception as e:
        fallback = _headline_fallback(news, reason=f"{provider} call failed")
        return Narration(
            text=fallback.text,
            generated_by=fallback.generated_by,
            sources=fallback.sources,
            error=f"{type(e).__name__}: {str(e)[:150]}",
        )
