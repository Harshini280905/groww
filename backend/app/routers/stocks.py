"""Per-stock endpoints — details on demand for a single symbol."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..catalog import search as catalog_search
from ..db import get_db
from ..models import PriceTick, SignificantEventRow, SourceReadingRow
from ..narrator import narrate_event
from ..schemas import (
    InstrumentOut, NarrationOut, NewsItemOut, SignificantEventOut,
    SourceReadingOut, StockLatestOut,
)

router = APIRouter()


def _ensure_tz(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/search", response_model=list[InstrumentOut])
def search_instruments(q: str = "", limit: int = 8):
    """Typeahead over the instrument catalog.

    Exists because making a user guess the exact NSE ticker is a bad
    experience — "HDFC" should find HDFCBANK, "tata" should surface TCS and
    TATAMOTORS. Unauthenticated: this is public reference data, not
    user-specific.
    """
    results = catalog_search(q, limit=min(max(limit, 1), 20))
    return [
        InstrumentOut(symbol=i.symbol, name=i.name, sector=i.sector)
        for i in results
    ]


@router.get("/{symbol}/latest", response_model=StockLatestOut)
def latest(symbol: str, db: Session = Depends(get_db)):
    tick = db.query(PriceTick).filter_by(symbol=symbol.upper()).first()
    if tick is None:
        raise HTTPException(status_code=404, detail=f"No data yet for {symbol.upper()}")
    fetched = _ensure_tz(tick.fetched_at)
    return StockLatestOut(
        symbol=tick.symbol,
        price=tick.price,
        volume=tick.volume,
        tier=tick.tier,
        confidence=round(tick.confidence, 3),
        fetched_at=fetched,
        staleness_secs=round((datetime.now(timezone.utc) - fetched).total_seconds(), 1),
    )


@router.get("/{symbol}/events", response_model=list[SignificantEventOut])
def events(symbol: str, limit: int = 50, db: Session = Depends(get_db)):
    rows = (
        db.query(SignificantEventRow)
        .filter_by(symbol=symbol.upper())
        .order_by(SignificantEventRow.ts.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return rows


@router.get("/{symbol}/source-readings", response_model=list[SourceReadingOut])
def source_readings(symbol: str, limit: int = 20, db: Session = Depends(get_db)):
    """The 'why did confidence drop to 0.5?' drill-down."""
    rows = (
        db.query(SourceReadingRow)
        .filter_by(symbol=symbol.upper())
        .order_by(SourceReadingRow.fetched_at.desc())
        .limit(min(max(limit, 1), 100))
        .all()
    )
    return rows


@router.post("/{symbol}/events/{event_id}/narrate", response_model=NarrationOut)
async def narrate_event_endpoint(symbol: str, event_id: int, db: Session = Depends(get_db)):
    """§11 AI boundary in practice: this endpoint ONLY reads a
    SignificantEventRow that already exists — the price, direction, and
    z-score it passes to the narrator are the confirmed facts from the
    deterministic pipeline. The narrator cannot alter them; it can only
    explain them, cited, or say plainly that it has nothing to cite.
    """
    row = (
        db.query(SignificantEventRow)
        .filter_by(id=event_id, symbol=symbol.upper())
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Significant event not found")

    return_pct = (
        (row.price_after - row.price_before) / row.price_before * 100
        if row.price_before > 0 else 0.0
    )

    # narrate_event does blocking I/O (yfinance + optionally the Anthropic
    # API) — run it off the event loop so one slow narration can't stall
    # every other request this process is handling.
    result = await asyncio.to_thread(
        narrate_event,
        symbol=row.symbol,
        direction=row.direction,
        return_pct=return_pct,
        z_score=row.z_score,
        confidence=row.confidence,
        tier="verified" if row.confidence >= 0.80 else "best_available",
    )

    return NarrationOut(
        text=result.text,
        generated_by=result.generated_by,
        sources=[
            NewsItemOut(title=s.title, publisher=s.publisher, link=s.link, published_at=s.published_at)
            for s in result.sources
        ],
        model=result.model,
        error=result.error,
    )
