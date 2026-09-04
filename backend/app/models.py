"""ORM models — the persistent shape referenced in architecture blueprint §05.

Split of concerns:
  * User / WatchlistItem are the only places a user identity appears.
    Everything about market data is symbol-keyed and shared across users —
    this is the "compute once per symbol, fan out to N users" story from §07.
  * SourceReading persists RAW per-source responses (pre-reconciliation) so
    post-hoc audits can always reconstruct what each source claimed
    independently of what the reconciler concluded.
  * PriceTick is a latest-only projection keyed by symbol — one row per
    symbol, overwritten each poll. Read path targets this.
  * SignificantEventRow is the append-only "commit log" the diff-since-last-
    visit endpoint replays.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    watchlist_items: Mapped[list["WatchlistItem"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    intent_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    user: Mapped[User] = relationship(back_populates="watchlist_items")

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uix_user_symbol"),
    )


class DailyBar(Base):
    """Historical OHLCV — feeds the trailing-volatility calc in significance.py."""
    __tablename__ = "daily_bars"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uix_symbol_date"),
    )


class PriceTick(Base):
    """Latest-known price per symbol (one row per symbol, overwritten per poll)."""
    __tablename__ = "price_ticks"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    price: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    tier: Mapped[str] = mapped_column(String(32), default="unconfirmed")


class SignificantEventRow(Base):
    """The append-only 'commit log' — replayed by the diff-since-last-visit endpoint."""
    __tablename__ = "significant_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    z_score: Mapped[float] = mapped_column(Float)
    volume_ratio: Mapped[float] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String(8))
    price_before: Mapped[float] = mapped_column(Float)
    price_after: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class SourceReadingRow(Base):
    """Raw per-source readings — audit trail, pre-reconciliation."""
    __tablename__ = "source_readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32))
    price: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    latency_ms: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
