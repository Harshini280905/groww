"""Notification distribution & coalescing — blueprint §08, hackathon-scoped.

The full production design (reverse index in Redis, per-user inbox as a
sorted set, a scheduled flusher, FCM/APNs delivery) is documented in the
architecture blueprint. This module implements the same SHAPE with
process-local structures, so the pattern is real and demonstrable without
standing up Redis and push infrastructure for a 72-hour build:

  * Reverse index   — symbol -> watchers, queried from the DB per event
                       (§08.1's SADD/SREM becomes a WHERE symbol = ? scan;
                       same O(watchers-of-this-symbol) cost shape).
  * Priority tiers  — P0 immediate / P1 batched / P2 digest (§08.3).
  * WebSocket fanout — connected clients get pushed immediately. With a
                       handful of demo users there is nothing to coalesce
                       yet, but the priority tagging and per-user routing
                       are the real thing, not a stub.

Swap-in path to production: replace `_watchers_for_symbol`'s DB query with
a Redis SET lookup, and `ConnectionManager.push` with a queued-inbox flush
job on a timer. The call site — `notify_significant_event`, invoked once
per confirmed event from pipeline.py — does not change.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from fastapi import WebSocket
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from .significance import SignificantEvent

log = logging.getLogger(__name__)


class Priority(str, Enum):
    """§08.3 — governs urgency, not delivery mechanism (both P0 and P1 use
    the same WebSocket push here; in production P1/P2 would instead land in
    the coalescing inbox and wait for the flush window)."""
    P0_IMMEDIATE = "P0"    # circuit hit, |z|>4, or own_it-tagged with |z|>3
    P1_BATCHED = "P1"      # normal significant event, |z|>2
    P2_DIGEST = "P2"       # interesting-but-not-alerting


def priority_for(event: "SignificantEvent", intent_tag: str | None) -> Priority:
    az = abs(event.z_score)
    if az >= 4.0 or (intent_tag == "own_it" and az >= 3.0):
        return Priority.P0_IMMEDIATE
    if az >= 2.0:
        return Priority.P1_BATCHED
    return Priority.P2_DIGEST


class ConnectionManager:
    """Per-user WebSocket registry. A user can hold multiple live
    connections (multiple tabs/devices) — the same "state persists across
    sessions/devices" requirement from the brief extends naturally to
    "a live push reaches every open session", not just one."""

    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = {}

    async def connect(self, user_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.setdefault(user_id, set()).add(ws)

    def disconnect(self, user_id: int, ws: WebSocket) -> None:
        conns = self._connections.get(user_id)
        if conns:
            conns.discard(ws)
            if not conns:
                self._connections.pop(user_id, None)

    async def push(self, user_id: int, payload: dict) -> int:
        """Send to every live connection for a user. Returns delivery count."""
        conns = list(self._connections.get(user_id, ()))
        if not conns:
            return 0
        message = json.dumps(payload)
        sent = 0
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(message)
                sent += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(user_id, ws)
        return sent

    def connection_count(self, user_id: int) -> int:
        return len(self._connections.get(user_id, ()))


manager = ConnectionManager()


def _watchers_for_symbol(db: Session, symbol: str) -> list[tuple[int, str | None]]:
    """§08.1's reverse index, DB-backed for the demo instead of a Redis SET —
    same lookup shape, same cost bound (proportional to watchers of THIS
    symbol, not to total users in the system)."""
    from .models import WatchlistItem
    rows = (
        db.query(WatchlistItem.user_id, WatchlistItem.intent_tag)
        .filter(WatchlistItem.symbol == symbol)
        .all()
    )
    return [(r[0], r[1]) for r in rows]


async def notify_significant_event(db: Session, event: "SignificantEvent") -> int:
    """Fan out one confirmed significant event to every current watcher.

    Called exactly once, from pipeline.poll_and_detect, right after the
    SignificantEventRow is committed — a notification can never be sent for
    an event that is not already durably recorded.
    """
    watchers = _watchers_for_symbol(db, event.symbol)
    total_sent = 0
    for user_id, intent_tag in watchers:
        priority = priority_for(event, intent_tag)
        payload = {
            "type": "significant_event",
            "priority": priority.value,
            "symbol": event.symbol,
            "direction": event.direction,
            "z_score": round(event.z_score, 2),
            "price_before": event.price_before,
            "price_after": event.price_after,
            "confidence": round(event.confidence, 2),
            "note": event.note,
            "ts": event.ts.isoformat() if isinstance(event.ts, datetime) else str(event.ts),
        }
        total_sent += await manager.push(user_id, payload)
    log.info(
        "notify: %s -> %d watcher(s), %d live delivery", event.symbol, len(watchers), total_sent
    )
    return total_sent
