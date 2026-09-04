"""Watchlist endpoints — the diff-since-last-visit engine.

Blueprint anchors:
  §01 — "meaningfully changed since they last checked" — the reason last_seen_at exists.
  §06.3 — Tier is surfaced on every row so the UI can render confidence honestly.
  §10 — Reads are cache-shaped: one PriceTick lookup + one event scan per item,
        no live cross-user aggregation. Bulk last_seen_at update is one round-trip.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..catalog import get as catalog_get
from ..db import get_db
from ..models import DailyBar, PriceTick, SignificantEventRow, SourceReadingRow, User, WatchlistItem
from ..schemas import BiggestEventOut, DiffSummaryOut, WatchlistItemCreate

router = APIRouter()


def _ensure_tz(dt: datetime) -> datetime:
    """SQLite loses timezone info on round-trip — normalize back to UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _explain_missing_data(db: Session, symbol: str) -> str:
    """Turn the raw per-source failures into one plain-English sentence.

    The pipeline always records WHY each source failed (SourceReadingRow).
    Without this, a user typing a non-NSE ticker just sees a blank card and
    can't tell whether the app is broken, the symbol is wrong, or the market
    is closed. The system knows the difference — it should say so.
    """
    latest = (
        db.query(SourceReadingRow)
        .filter_by(symbol=symbol)
        .order_by(SourceReadingRow.fetched_at.desc())
        .limit(6)
        .all()
    )
    if not latest:
        return "Not polled yet — click “Poll now” to fetch."

    # Most recent attempt per source
    errors: dict[str, str] = {}
    for row in latest:
        errors.setdefault(row.source, row.error or "")

    not_found = {"symbol_not_found", "unknown_scrip_code", "empty_price"}
    reachable_failures = {"http_403", "timeout", "circuit_open"}

    said_not_found = [s for s, e in errors.items() if e in not_found]
    said_unreachable = [s for s, e in errors.items() if e in reachable_failures
                        or e.startswith("http_")]

    # If every source that could actually answer says "no such instrument",
    # the symbol itself is the problem — not the plumbing.
    if said_not_found and not any(e == "" for e in errors.values()):
        if len(said_not_found) >= len([s for s in errors if s != "nse"]):
            return (
                f"“{symbol}” doesn’t look like an NSE-listed symbol. This app "
                f"covers Indian equities (NSE/BSE) — try TCS, RELIANCE, INFY, "
                f"HDFCBANK or SBIN."
            )

    if said_unreachable and not said_not_found:
        return (
            "All data sources are temporarily unreachable. The price shown "
            "(if any) is the last confirmed value — nothing has been fabricated."
        )

    detail = ", ".join(f"{s}: {e or 'ok'}" for s, e in errors.items())
    return f"No usable price could be confirmed ({detail})."


def _day_change(db: Session, symbol: str, current_price: float) -> tuple[Optional[float], Optional[float]]:
    """(prev_close, day_change_pct) from the most recent stored daily bar.

    This is what turns a bare "₹2,302.45" into "₹2,302.45, +1.2% today" —
    a number with a reference point instead of a number floating in space.
    """
    if current_price <= 0:
        return None, None
    bar = (
        db.query(DailyBar)
        .filter_by(symbol=symbol)
        .order_by(DailyBar.date.desc())
        .first()
    )
    if bar is None or bar.close <= 0:
        return None, None
    return bar.close, round((current_price - bar.close) / bar.close * 100, 2)


def _diff_summary(db: Session, item: WatchlistItem) -> DiffSummaryOut:
    tick = db.query(PriceTick).filter_by(symbol=item.symbol).first()
    now = datetime.now(timezone.utc)
    inst = catalog_get(item.symbol)
    name = inst.name if inst else None
    sector = inst.sector if inst else None

    if tick is None:
        return DiffSummaryOut(
            id=item.id, symbol=item.symbol, company_name=name, sector=sector,
            intent_tag=item.intent_tag,
            status="no_data_yet",
            data_issue=_explain_missing_data(db, item.symbol),
        )

    # A tick exists but nothing could be verified — explain rather than
    # rendering a silent, empty card.
    if tick.price <= 0 or tick.tier == "unconfirmed":
        return DiffSummaryOut(
            id=item.id, symbol=item.symbol, company_name=name, sector=sector,
            intent_tag=item.intent_tag,
            current_price=tick.price if tick.price > 0 else None,
            tier=tick.tier, confidence=round(tick.confidence, 3),
            staleness_secs=round((now - _ensure_tz(tick.fetched_at)).total_seconds(), 1),
            status="no_data_yet",
            data_issue=_explain_missing_data(db, item.symbol),
        )

    fetched = _ensure_tz(tick.fetched_at)
    staleness = (now - fetched).total_seconds()
    prev_close, day_change_pct = _day_change(db, item.symbol, tick.price)

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
            id=item.id, symbol=item.symbol, company_name=name, sector=sector,
            intent_tag=item.intent_tag,
            current_price=tick.price, tier=tick.tier,
            confidence=round(tick.confidence, 3),
            staleness_secs=round(staleness, 1),
            prev_close=prev_close, day_change_pct=day_change_pct,
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
        id=item.id, symbol=item.symbol, company_name=name, sector=sector,
        intent_tag=item.intent_tag,
        current_price=tick.price, tier=tick.tier,
        confidence=round(tick.confidence, 3),
        staleness_secs=round(staleness, 1),
        prev_close=prev_close, day_change_pct=day_change_pct,
        status="significant_change",
        event_count=len(events),
        net_drift_pct=round(net_drift, 2),
        biggest_event=BiggestEventOut(
            id=biggest.id,
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

    # Advance the checkpoint ONLY for items that actually surfaced events.
    #
    # This started as a write on every single read, which made GET
    # /api/watchlist a writing endpoint — and load testing showed exactly
    # what that costs: throughput flatlined at ~47 rps regardless of
    # concurrency and collapsed entirely at 50 concurrent users, because
    # SQLite serialises writers. The same server serving a read-only
    # endpoint sustained ~170 rps and survived 50 users cleanly.
    #
    # The fix is semantic, not a trick: last_seen_at records what the user
    # has SEEN. If nothing was surfaced, there is nothing to acknowledge and
    # the window can stay open — the next genuine event still lands after
    # the checkpoint either way, so the diff a user sees is unchanged.
    # Roughly 91% of views surface nothing (measured in prove_detection.py),
    # so this removes the write from the overwhelming majority of requests.
    seen_ids = [
        s.id for s in summaries
        if s.status == "significant_change"
    ]
    if seen_ids:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        (
            db.query(WatchlistItem)
            .filter(WatchlistItem.id.in_(seen_ids))
            .update({WatchlistItem.last_seen_at: now_naive}, synchronize_session=False)
        )
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
