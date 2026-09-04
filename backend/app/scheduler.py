"""Background poller — the production replacement for the manual
`POST /api/dev/populate/{symbol}` trigger.

Blueprint anchors:
  §04/§07 — Market-hours-aware, batched, bounded polling: no polling outside
            NSE trading hours (near-zero ingestion cost ~17 of 24 hours a
            day); `distinct_watched_symbols` dedupes across every watchlist,
            so cost scales with symbol count (hard-capped by the exchange,
            ~2,000 NSE-listed names), never with user count.

One asyncio job on a fixed interval via APScheduler. Deliberately sequential
per symbol rather than firing all of them concurrently — the reconciler
already fans out per-symbol internally across three sources; adding a second
layer of concurrency here would just race the same rate limits harder for
no benefit at hackathon scale.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .db import SessionLocal
from .pipeline import distinct_watched_symbols, poll_and_detect

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))


def market_is_open(now: datetime | None = None) -> bool:
    """NSE trading hours, Mon-Fri, 09:15-15:30 IST. Pure function — takes
    `now` as a parameter so it's testable without mocking the clock."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5:            # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


async def poll_all_watched_symbols() -> None:
    """One full cycle: skip if market closed, else poll every distinct
    watched symbol through the shared pipeline (same code path the manual
    dev trigger uses)."""
    if not market_is_open():
        log.info("scheduler: market closed, skipping poll cycle")
        return

    db = SessionLocal()
    try:
        symbols = distinct_watched_symbols(db)
    finally:
        db.close()

    if not symbols:
        log.info("scheduler: no watched symbols, nothing to poll")
        return

    log.info("scheduler: polling %d distinct symbol(s)", len(symbols))
    for symbol in symbols:
        db = SessionLocal()
        try:
            await poll_and_detect(symbol, db)
        except Exception:
            log.exception("scheduler: poll failed for %s", symbol)
        finally:
            db.close()


scheduler = AsyncIOScheduler(timezone=str(IST))


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        poll_all_watched_symbols,
        "interval",
        minutes=POLL_INTERVAL_MINUTES,
        id="poll_all_watched_symbols",
        replace_existing=True,
        next_run_time=datetime.now(IST),   # also fire once immediately on startup
    )
    scheduler.start()
    log.info("scheduler: started, interval=%dmin", POLL_INTERVAL_MINUTES)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("scheduler: stopped")
