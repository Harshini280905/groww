"""Dev-only endpoints — put the whole pipeline behind one HTTP call.

Without waiting for the scheduler's next cycle, the demo needs a way to
populate real data into the DB on demand so /watchlist and /stocks/...
return something meaningful immediately. This router is that hook — it is
now a thin wrapper around `pipeline.poll_and_detect`, the exact same code
path the background scheduler (scheduler.py) calls on its interval. There
is no separate "demo version" of the pipeline; this just triggers it early.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DailyBar
from ..pipeline import poll_and_detect
from ..schemas import PopulateResultOut, SourceReadingOut

router = APIRouter()


@router.post("/populate/{symbol}", response_model=PopulateResultOut)
async def populate(symbol: str, db: Session = Depends(get_db)):
    """Run one full poll -> reconcile -> detect -> persist cycle for a symbol."""
    quote, result, event_recorded = await poll_and_detect(symbol, db)

    return PopulateResultOut(
        symbol=quote.symbol,
        resolved_price=quote.price,
        tier=quote.tier.value,
        confidence=round(quote.confidence, 3),
        readings=[
            SourceReadingOut(
                source=r.source, price=r.price, volume=r.volume,
                fetched_at=r.fetched_at, latency_ms=round(r.latency_ms, 1),
                error=r.error,
            )
            for r in quote.readings
        ],
        significance_reason=result.reason.value,
        is_significant=result.is_significant,
        event_recorded=event_recorded,
        z_score=round(result.z_score, 3) if result.z_score is not None else None,
        todays_return_pct=(
            round(result.todays_return_pct, 3)
            if result.todays_return_pct is not None else None
        ),
    )


@router.get("/history/{symbol}")
def stored_history(symbol: str, db: Session = Depends(get_db), limit: int = 60):
    """Show what daily bars we've stored for a symbol."""
    rows = (
        db.query(DailyBar)
        .filter_by(symbol=symbol.upper())
        .order_by(DailyBar.date.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    if not rows:
        raise HTTPException(404, "No history stored for this symbol")
    return [
        {
            "date": r.date.isoformat(),
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume,
        }
        for r in rows
    ]
