"""Pydantic response models — the shape the frontend actually receives.

These are the API surface, not the storage shape. Deliberately different
from models.py: they carry computed fields (staleness_secs, biggest_event,
net_drift_pct) that live only in the response.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class WatchlistItemCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    intent_tag: Optional[str] = Field(default=None, max_length=32)


class BiggestEventOut(BaseModel):
    """The most-notable event within a diff window — one per diff summary."""
    ts: datetime
    z_score: float
    return_pct: float
    volume_ratio: float
    direction: str
    note: Optional[str] = None


class DiffSummaryOut(BaseModel):
    """Diff-since-last-visit for one watchlist item — the load-bearing response.

    `status` is the primary UI-facing field:
      "no_data_yet"           — symbol added but no poll has landed yet
      "no_significant_change" — polls have happened, nothing crossed threshold
      "significant_change"    — one or more SignificantEvents since last_seen_at
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    intent_tag: Optional[str] = None
    current_price: Optional[float] = None
    tier: str = "unconfirmed"
    confidence: float = 0.0
    staleness_secs: Optional[float] = None
    status: str
    event_count: int = 0
    net_drift_pct: Optional[float] = None
    biggest_event: Optional[BiggestEventOut] = None


class SignificantEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    ts: datetime
    z_score: float
    volume_ratio: float
    direction: str
    price_before: float
    price_after: float
    confidence: float
    note: Optional[str] = None


class StockLatestOut(BaseModel):
    symbol: str
    price: float
    volume: Optional[float] = None
    tier: str
    confidence: float
    fetched_at: datetime
    staleness_secs: float


class SourceReadingOut(BaseModel):
    """Per-source reading — the "why is confidence X" drill-down."""
    model_config = ConfigDict(from_attributes=True)

    source: str
    price: float
    volume: Optional[float] = None
    fetched_at: datetime
    latency_ms: float
    error: Optional[str] = None


class PopulateResultOut(BaseModel):
    """Result of the dev populate endpoint — makes the pipeline visible."""
    symbol: str
    resolved_price: float
    tier: str
    confidence: float
    readings: list[SourceReadingOut]
    significance_reason: str
    is_significant: bool
    event_recorded: bool
    z_score: Optional[float] = None
    todays_return_pct: Optional[float] = None
