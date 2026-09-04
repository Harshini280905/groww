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


class InstrumentOut(BaseModel):
    """A searchable instrument from the catalog."""
    symbol: str
    name: str
    sector: str


class BiggestEventOut(BaseModel):
    """The most-notable event within a diff window — one per diff summary."""
    id: int
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
    # Human-readable company name. A bare ticker means nothing to someone
    # new — "TCS" should read as "Tata Consultancy Services".
    company_name: Optional[str] = None
    sector: Optional[str] = None
    intent_tag: Optional[str] = None
    current_price: Optional[float] = None
    # Previous close and today's move. Without these, the rupee figure on a
    # card is an unlabelled number with no reference point — the single most
    # confusing thing a new user hits.
    prev_close: Optional[float] = None
    day_change_pct: Optional[float] = None
    tier: str = "unconfirmed"
    confidence: float = 0.0
    staleness_secs: Optional[float] = None
    status: str
    event_count: int = 0
    net_drift_pct: Optional[float] = None
    biggest_event: Optional[BiggestEventOut] = None
    # Plain-English explanation when there is no usable price. The system
    # always knows WHY a symbol has no data (not NSE-listed / all sources
    # down / never polled) — staying silent about it is a UX bug, not a
    # feature. Null whenever data is fine.
    data_issue: Optional[str] = None


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


class NewsItemOut(BaseModel):
    title: str
    publisher: str
    link: str
    published_at: Optional[str] = None


class NarrationOut(BaseModel):
    """Response of POST /stocks/{symbol}/events/{event_id}/narrate.

    `generated_by` tells the truth about how `text` was produced:
      "claude-api"        — actually synthesized by an LLM from cited headlines
      "headline-fallback" — no ANTHROPIC_API_KEY configured; direct headline lookup
      "no-news-found"     — nothing to cite, nothing synthesized
    A UI must surface this field, not just `text` — presenting a headline
    lookup as if it were AI-generated would violate the §11 boundary this
    module exists to enforce.
    """
    text: str
    generated_by: str
    sources: list[NewsItemOut] = []
    model: Optional[str] = None
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
