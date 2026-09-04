"""Watchlist endpoints — the diff-since-last-visit engine.

Blueprint anchors:
  §01 — "meaningfully changed since they last checked" — the reason last_seen_at exists.
  §06.3 — Tier is surfaced on every row so the UI can render confidence honestly.
  §10 — Reads are cache-shaped: one PriceTick lookup + one event scan per item,
        no live cross-user aggregation. Bulk last_seen_at update is one round-trip.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import PriceTick, SignificantEventRow, User, WatchlistItem
from ..schemas import BiggestEventOut, DiffSummaryOut, WatchlistItemCreate

router = APIRouter()


def _ensure_tz(dt: datetime) -> datetime:
    """SQLite loses timezone info on round-trip — normalize back to UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _diff_summary(db: Session, item: WatchlistItem) -> DiffSummaryOut:
    tick = db.query(PriceTick).filter_by(symbol=item.symbol).first()
    now = datetime.now(timezone.utc)

    if tick is None:
        return DiffSummaryOut(
            id=item.id, symbol=item.symbol, intent_tag=item.intent_tag,
            status="no_data_yet",
        )

    fetched = _ensure_tz(tick.fetched_at)
    staleness = (now - fetched).total_seconds()

    events = (
        db.query(SignificantEventRow)
        .filter(
            SignificantEventRow.symbol == item.symbol,
            SignificantEventRow.ts > _ensure_tz(item.last_seen_at).replace(tzinfo=None),
        )
        .order_by(SignificantEventRow.ts.asc())
        .all()
    )

    if not events:
        return DiffSummaryOut(
            id=item.id, symbol=item.symbol, intent_tag=item.intent_tag,
            current_price=tick.price, tier=tick.tier,
            confidence=round(tick.confidence, 3),
            staleness_secs=round(staleness, 1),
            status="no_significant_change",
        )

    biggest = max(events, key=lambda e: abs(e.z_score))
    first, last = events[0], events[-1]
    net_drift = (
        (last.price_after - first.price_before) / first.price_before * 100
        if first.price_before > 0 else 0.0
    )
    biggest_return = (
        (biggest.price_after - biggest.price_before) / biggest.price_before * 100
        if biggest.price_before > 0 else 0.0
    )
    return DiffSummaryOut(
        id=item.id, symbol=item.symbol, intent_tag=item.intent_tag,
        current_price=tick.price, tier=tick.tier,
        confidence=round(tick.confidence, 3),
        staleness_secs=round(staleness, 1),
        status="significant_change",
        event_count=len(events),
        net_drift_pct=round(net_drift, 2),
        biggest_event=BiggestEventOut(
            ts=_ensure_tz(biggest.ts),
            z_score=round(biggest.z_score, 2),
            return_pct=round(biggest_return, 2),
            volume_ratio=round(biggest.volume_ratio, 2),
            direction=biggest.direction,
            note=biggest.note,
        ),
    )


@router.post("", response_model=DiffSummaryOut)
def add_to_watchlist(
    body: WatchlistItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    symbol = body.symbol.upper()
    existing = db.query(WatchlistItem).filter_by(user_id=current_user.id, symbol=symbol).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"{symbol} already on watchlist")
    item = WatchlistItem(user_id=current_user.id, symbol=symbol, intent_tag=body.intent_tag)
    db.add(item)
    db.commit()
    db.refresh(item)
    return _diff_summary(db, item)


@router.get("", response_model=list[DiffSummaryOut])
def get_watchlist(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items = (
        db.query(WatchlistItem)
        .filter_by(user_id=current_user.id)
        .order_by(WatchlistItem.added_at.desc())
        .all()
    )
    summaries = [_diff_summary(db, item) for item in items]
    # Bump last_seen_at AFTER computing summaries — the diff represents events
    # that happened up to this GET; next GET starts its window from here.
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    for item in items:
        item.last_seen_at = now_naive
    db.commit()
    return summaries


@router.delete("/{item_id}")
def remove_from_watchlist(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = db.query(WatchlistItem).filter_by(id=item_id, user_id=current_user.id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    db.delete(item)
    db.commit()
    return {"ok": True, "deleted_id": item_id}
