"""Shared poll -> reconcile -> detect -> persist pipeline.

Used by both the manual dev trigger (routers/dev.py) and the background
scheduler (scheduler.py) — one code path, two callers, so there is no drift
between "what the demo button does" and "what the real poller does".

This is exactly the refactor the architecture blueprint's §07 scaling story
depends on: `distinct_watched_symbols` dedupes across every user's
watchlist, so the scheduler's ingestion cost is bounded by symbol count,
not user count, regardless of which caller triggers a poll.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .history import fetch_daily_bars
from .market_data import Reconciler, ReconciledQuote
from .models import DailyBar, PriceTick, SignificantEventRow, SourceReadingRow, WatchlistItem
from .notifications import notify_significant_event
from .significance import SignificanceResult, detect_significance
from .sources.bse import BSESource
from .sources.nse import NSEDirectSource
from .sources.yahoo import YahooSource

# One long-lived reconciler for the process — keeps warmed sessions +
# circuit-breakers alive across requests/poll-cycles. Real production would
# inject via FastAPI dependencies, but a module-level singleton is fine here.
# NSE blocks datacenter IPs (403 from any cloud host). Off unless explicitly
# enabled — see get_reconciler() for the full rationale.
ENABLE_NSE = os.getenv("ENABLE_NSE", "0") == "1"

_yahoo: Optional[YahooSource] = None
_nse: Optional[NSEDirectSource] = None
_bse: Optional[BSESource] = None
_reconciler: Optional[Reconciler] = None


def get_reconciler() -> Reconciler:
    """Build the source set once per process.

    NSE is DISABLED BY DEFAULT (`ENABLE_NSE=1` to turn it on). Their bot
    detection blocks datacenter IPs outright — from Render, or any cloud
    host, every request returns 403. Shipping a source that always fails
    is worse than not shipping it: it drags `coverage` down (2/3 instead of
    2/2), so every quote scores lower for a reason the user can do nothing
    about, and it fills the per-source drill-down with noise.

    The adapter and its tests stay in the tree — the work is real and it
    runs fine from a residential IP or behind a proxy. This is a deployment
    switch, not a deletion.
    """
    global _yahoo, _nse, _bse, _reconciler
    if _reconciler is None:
        _yahoo = YahooSource()
        _bse = BSESource()
        # Yahoo (US aggregator) + BSE (India's older exchange, separate
        # infrastructure from NSE) are genuinely independent — both must
        # agree for a quote to reach VERIFIED.
        sources = [_yahoo, _bse]
        if ENABLE_NSE:
            _nse = NSEDirectSource()
            sources.insert(1, _nse)
        _reconciler = Reconciler(sources)
    return _reconciler


async def shutdown_reconciler() -> None:
    """Called from FastAPI lifespan on shutdown."""
    global _nse, _bse
    if _nse is not None:
        await _nse.close()
    if _bse is not None:
        await _bse.close()


def _ensure_naive_utc(dt: datetime) -> datetime:
    """SQLite DateTime is naive; standardise on naive-UTC for storage."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _upsert_daily_bars(db: Session, symbol: str, bars: list[dict]) -> int:
    written = 0
    for bar in bars:
        naive_date = _ensure_naive_utc(bar["date"])
        row = db.query(DailyBar).filter_by(symbol=symbol, date=naive_date).first()
        if row is None:
            row = DailyBar(
                symbol=symbol, date=naive_date,
                open=bar["open"], high=bar["high"], low=bar["low"],
                close=bar["close"], volume=bar["volume"],
            )
            db.add(row)
            written += 1
        else:
            row.open, row.high, row.low = bar["open"], bar["high"], bar["low"]
            row.close, row.volume = bar["close"], bar["volume"]
    db.commit()
    return written


def _upsert_price_tick(db: Session, quote: ReconciledQuote) -> None:
    tick = db.query(PriceTick).filter_by(symbol=quote.symbol).first()
    fetched_naive = _ensure_naive_utc(quote.resolved_at)
    if tick is None:
        tick = PriceTick(
            symbol=quote.symbol, price=quote.price, volume=quote.volume,
            fetched_at=fetched_naive, confidence=quote.confidence,
            tier=quote.tier.value,
        )
        db.add(tick)
    else:
        tick.price = quote.price
        tick.volume = quote.volume
        tick.fetched_at = fetched_naive
        tick.confidence = quote.confidence
        tick.tier = quote.tier.value
    db.commit()


def _persist_source_readings(db: Session, quote: ReconciledQuote) -> None:
    for r in quote.readings:
        row = SourceReadingRow(
            symbol=r.symbol, source=r.source, price=r.price, volume=r.volume,
            fetched_at=_ensure_naive_utc(r.fetched_at),
            latency_ms=r.latency_ms, error=r.error,
        )
        db.add(row)
    db.commit()


async def poll_and_detect(
    symbol: str, db: Session
) -> tuple[ReconciledQuote, SignificanceResult, bool]:
    """One full poll -> reconcile -> detect -> persist cycle for one symbol.

    Returns (quote, significance_result, event_recorded). If an event fired,
    watchers are notified (§08) — exactly once, and only after the event is
    durably committed, so a push can never precede its own audit trail.
    """
    symbol = symbol.upper()
    reconciler = get_reconciler()

    quote = await reconciler.reconcile(symbol)
    _persist_source_readings(db, quote)
    _upsert_price_tick(db, quote)

    bars = await fetch_daily_bars(symbol)
    if bars:
        _upsert_daily_bars(db, symbol, bars[:-1])   # exclude today (== live price)

    hist_rows = (
        db.query(DailyBar)
        .filter_by(symbol=symbol)
        .order_by(DailyBar.date.asc())
        .all()
    )
    closes = [r.close for r in hist_rows]
    volumes = [r.volume for r in hist_rows]

    result = detect_significance(quote, daily_closes=closes, daily_volumes=volumes)

    event_recorded = False
    if result.is_significant and result.event is not None:
        row = SignificantEventRow(
            symbol=result.event.symbol,
            ts=_ensure_naive_utc(result.event.ts),
            z_score=result.event.z_score,
            volume_ratio=result.event.volume_ratio,
            direction=result.event.direction,
            price_before=result.event.price_before,
            price_after=result.event.price_after,
            confidence=result.event.confidence,
            note=result.event.note,
        )
        db.add(row)
        db.commit()
        event_recorded = True
        await notify_significant_event(db, result.event)

    return quote, result, event_recorded


def distinct_watched_symbols(db: Session) -> list[str]:
    """§07 scaling bound: distinct symbols across ALL users' watchlists, not
    per-user. This is what makes ingestion cost scale with symbol count
    (capped by the exchange, ~2,000 NSE-listed names) rather than user count.
    """
    rows = db.query(WatchlistItem.symbol).distinct().all()
    return [r[0] for r in rows]
