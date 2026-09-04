"""Per-stock endpoints — details on demand for a single symbol."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PriceTick, SignificantEventRow, SourceReadingRow
from ..schemas import SignificantEventOut, SourceReadingOut, StockLatestOut

router = APIRouter()


def _ensure_tz(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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
