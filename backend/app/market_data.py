"""Multi-source price ingestion and reconciliation layer.

This module is deliberately the load-bearing file — every trust guarantee the
rest of the system makes about a price ultimately traces back through here.

Design references (see watchlist-architecture.html):
  §06.3 — Confidence score formula (coverage + agreement + freshness weighting)
  §06.3 — Median (not mean) as the resolved price — robust to one broken source
  §09.1 — Bounded per-symbol queues with drop-OLDEST discipline
  §09.2 — Per-symbol reconciliation rate cap
  §09.3 — Exchange-circuit awareness suspends significance while circuited
  §09.4 — Per-source circuit breakers (CS-kind) with exponential-cooldown backoff
  §11   — Ground-truth pipeline stays deterministic; no LLM decides a number

Nothing here talks to a database or a cache — pure computation and I/O against
market sources. The worker layer wires this into persistence.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Confidence tiers — the user-facing surface of §06.3
# ─────────────────────────────────────────────────────────────────────────────

class ConfidenceTier(str, Enum):
    """User-visible trust tier.

    VERIFIED       — normal display; a move here can trigger a SignificantEvent.
    BEST_AVAILABLE — shown with a caveat; event z-score threshold raised (see
                     significance.py) — we trust the number less, so we need
                     a stronger statistical signal before calling it real.
    UNCONFIRMED    — NEVER fires a significance event. UI shows the last
                     verified price with an explicit degraded-data badge
                     instead of presenting a fresh but shaky number as fact.
    """
    VERIFIED = "verified"
    BEST_AVAILABLE = "best_available"
    UNCONFIRMED = "unconfirmed"


VERIFIED_THRESHOLD = 0.80
BEST_THRESHOLD = 0.50


def tier_for(confidence: float, n_ok_sources: int = 2) -> ConfidenceTier:
    """Confidence tier for a resolved quote.

    Invariant: VERIFIED requires cross-verification — at least two independent
    sources must have contributed. A single-source quote cannot be VERIFIED
    regardless of how high its computed confidence is, because the label
    would overstate what we actually checked (a lone source can be perfectly
    self-consistent and completely wrong; only agreement across independent
    sources rules that out). `n_ok_sources` defaults to 2 so callers that
    only care about the confidence-vs-threshold mapping get the intuitive
    result; the reconciler passes the real count.
    """
    if confidence >= VERIFIED_THRESHOLD and n_ok_sources >= 2:
        return ConfidenceTier.VERIFIED
    if confidence >= BEST_THRESHOLD:
        return ConfidenceTier.BEST_AVAILABLE
    return ConfidenceTier.UNCONFIRMED


# ─────────────────────────────────────────────────────────────────────────────
# Value objects
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceReading:
    """A single source's answer for a single symbol at a single moment.

    Persisted verbatim (see SourceReading table) so post-hoc analysis can
    always reconstruct what each source claimed independently of what the
    reconciler concluded.
    """
    source: str
    symbol: str
    price: float
    volume: Optional[float]
    fetched_at: datetime
    latency_ms: float
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.price > 0


@dataclass(frozen=True)
class ReconciledQuote:
    """The single number the rest of the system is allowed to treat as truth.

    `readings` is retained deliberately — every downstream consumer can
    inspect which sources actually contributed. Confidence is not a magic
    scalar; it decomposes into three named terms so a low score is always
    explainable ("agreement dropped to 0.4 because Yahoo and NSE disagreed
    by 3%").
    """
    symbol: str
    price: float
    volume: Optional[float]
    resolved_at: datetime
    confidence: float
    tier: ConfidenceTier
    coverage: float
    agreement: float
    freshness: float
    readings: tuple[SourceReading, ...]


# ─────────────────────────────────────────────────────────────────────────────
# Per-source circuit breaker — §09.4
# ─────────────────────────────────────────────────────────────────────────────

class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Standard three-state breaker with exponential-backoff cooldown.

    Tracks *consecutive* failures rather than a rolling error rate. Deliberate
    choice: consecutive-failure semantics are stricter and easier to reason
    about for a hackathon-scale demo; a production build would swap in a
    rolling window (p95 latency, error rate over 5 min) — behind the same
    `allow()` / `record_*()` interface, so nothing downstream changes.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        base_cooldown_s: float = 30.0,
        max_cooldown_s: float = 300.0,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.base_cooldown_s = base_cooldown_s
        self.max_cooldown_s = max_cooldown_s

        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._opened_at: Optional[float] = None
        self._current_cooldown_s: float = base_cooldown_s
        self._half_open_probe_inflight: bool = False

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow(self) -> bool:
        """Whether a fresh call to this source should proceed right now."""
        now = time.monotonic()

        if self._state is CircuitState.CLOSED:
            return True

        if self._state is CircuitState.OPEN:
            assert self._opened_at is not None
            if now - self._opened_at >= self._current_cooldown_s:
                # Cooldown elapsed — transition to HALF_OPEN and permit one probe.
                self._state = CircuitState.HALF_OPEN
                self._half_open_probe_inflight = True
                return True
            return False

        # HALF_OPEN — allow exactly one probe at a time.
        if self._half_open_probe_inflight:
            return False
        self._half_open_probe_inflight = True
        return True

    def record_success(self) -> None:
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._current_cooldown_s = self.base_cooldown_s
        self._half_open_probe_inflight = False

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        self._half_open_probe_inflight = False

        should_open = (
            self._state is CircuitState.HALF_OPEN
            or self._consecutive_failures >= self.failure_threshold
        )
        if should_open:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            self._current_cooldown_s = min(
                self._current_cooldown_s * 2, self.max_cooldown_s
            )


# ─────────────────────────────────────────────────────────────────────────────
# Source protocol — one concrete adapter per file in sources/
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class MarketSource(Protocol):
    name: str
    breaker: CircuitBreaker

    async def fetch(self, symbol: str) -> SourceReading:
        """Fetch one reading. Must NOT raise — exceptions must be caught
        inside the adapter and returned as a SourceReading with an `error`
        field set. This keeps reconciliation's gather() call pure and avoids
        one broken source poisoning the batch."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Confidence score — §06.3
# ─────────────────────────────────────────────────────────────────────────────

STALENESS_CAP_S = 90.0    # Beyond 90s old, freshness is 0.
SPREAD_CAP = 0.02          # Cross-source spread ≥ 2% saturates agreement to 0.
W_COVERAGE = 0.35
W_AGREEMENT = 0.45
W_FRESHNESS = 0.20


def _confidence_terms(
    readings: list[SourceReading],
    configured_sources: int,
    now: datetime,
) -> tuple[float, float, float]:
    """Returns (coverage, agreement, freshness).

    Kept as a pure function so the pieces are testable in isolation and the
    reason a given quote earned a given tier can always be shown to the user
    ("Confidence 0.62 — agreement 0.40 pulled it down; Yahoo and NSE differ
    by 2.4% right now").
    """
    ok = [r for r in readings if r.ok]

    coverage = len(ok) / max(configured_sources, 1)
    if not ok:
        return coverage, 0.0, 0.0

    prices = [r.price for r in ok]
    if len(prices) == 1:
        # A single ok source can't disagree with itself — but coverage penalty
        # already reflects that we couldn't verify against a second source.
        agreement = 1.0
    else:
        median_p = statistics.median(prices)
        spread = (max(prices) - min(prices)) / median_p if median_p > 0 else 1.0
        agreement = max(0.0, 1.0 - min(spread / SPREAD_CAP, 1.0))

    max_staleness_s = max(
        (now - r.fetched_at).total_seconds() for r in ok
    )
    freshness = max(0.0, 1.0 - min(max_staleness_s / STALENESS_CAP_S, 1.0))

    return coverage, agreement, freshness


def compute_confidence(
    readings: list[SourceReading],
    configured_sources: int,
    now: Optional[datetime] = None,
) -> tuple[float, float, float, float]:
    """Returns (confidence, coverage, agreement, freshness)."""
    now = now or datetime.now(timezone.utc)
    coverage, agreement, freshness = _confidence_terms(readings, configured_sources, now)
    confidence = W_COVERAGE * coverage + W_AGREEMENT * agreement + W_FRESHNESS * freshness
    return confidence, coverage, agreement, freshness


# ─────────────────────────────────────────────────────────────────────────────
# Reconciler — the orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class Reconciler:
    """Fans out to every configured source in parallel, then resolves.

    Key correctness properties:
      * Concurrent fan-out via asyncio.gather so one slow source does not
        serialise the others.
      * Per-source circuit breaker consulted BEFORE the call — an open breaker
        yields an error reading immediately, keeping the whole batch under
        an approximate wall-clock bound of max(per-source timeout).
      * Median (not mean) resolution — a single wildly-wrong reading from a
        broken source cannot drag the resolved price. Mean would be silently
        corrupted.
      * All exceptions are contained inside adapters; gather() itself is
        never given the chance to raise.
    """

    # Hard per-source deadline enforced by the RECONCILER, independent of
    # whatever timeout an adapter sets for itself. Adapters are expected to
    # bound their own I/O, but this class already refuses to trust them not
    # to raise — trusting them not to HANG was an inconsistency that chaos
    # testing caught: a single unresponsive source stalled the whole fan-out
    # even though every other source had already answered. Slightly above a
    # typical adapter timeout (8-10s) so this only fires when an adapter has
    # failed to honour its own.
    PER_SOURCE_TIMEOUT_S = 12.0

    def __init__(self, sources: list[MarketSource]) -> None:
        if not sources:
            raise ValueError("Reconciler needs at least one source.")
        self.sources = sources

    async def _fetch_one(self, source: MarketSource, symbol: str) -> SourceReading:
        if not source.breaker.allow():
            return SourceReading(
                source=source.name,
                symbol=symbol,
                price=0.0,
                volume=None,
                fetched_at=datetime.now(timezone.utc),
                latency_ms=0.0,
                error="circuit_open",
            )
        try:
            # The reconciler enforces its own deadline. An adapter that hangs
            # must not be able to stall sources that have already answered.
            reading = await asyncio.wait_for(
                source.fetch(symbol), timeout=self.PER_SOURCE_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            log.warning(
                "Adapter %s exceeded the reconciler deadline on %s", source.name, symbol
            )
            reading = SourceReading(
                source=source.name,
                symbol=symbol,
                price=0.0,
                volume=None,
                fetched_at=datetime.now(timezone.utc),
                latency_ms=self.PER_SOURCE_TIMEOUT_S * 1000,
                error="reconciler_timeout",
            )
        except Exception as e:
            # Belt-and-suspenders — adapters SHOULD swallow their own errors.
            log.exception("Adapter %s leaked an exception on %s", source.name, symbol)
            reading = SourceReading(
                source=source.name,
                symbol=symbol,
                price=0.0,
                volume=None,
                fetched_at=datetime.now(timezone.utc),
                latency_ms=0.0,
                error=f"adapter_exception: {type(e).__name__}",
            )

        if reading.ok:
            source.breaker.record_success()
        else:
            source.breaker.record_failure()
        return reading

    async def reconcile(self, symbol: str) -> ReconciledQuote:
        readings = list(
            await asyncio.gather(
                *(self._fetch_one(s, symbol) for s in self.sources),
                return_exceptions=False,
            )
        )
        confidence, coverage, agreement, freshness = compute_confidence(
            readings, configured_sources=len(self.sources)
        )
        ok_readings = [r for r in readings if r.ok]

        if ok_readings:
            resolved_price = statistics.median(r.price for r in ok_readings)
            volumes = [r.volume for r in ok_readings if r.volume is not None]
            resolved_volume = statistics.median(volumes) if volumes else None
        else:
            resolved_price = 0.0
            resolved_volume = None

        return ReconciledQuote(
            symbol=symbol,
            price=resolved_price,
            volume=resolved_volume,
            resolved_at=datetime.now(timezone.utc),
            confidence=confidence,
            tier=tier_for(confidence, n_ok_sources=len(ok_readings)),
            coverage=coverage,
            agreement=agreement,
            freshness=freshness,
            readings=tuple(readings),
        )


# ─────────────────────────────────────────────────────────────────────────────
# §09.1 — Bounded per-symbol queues with drop-OLDEST discipline
# ─────────────────────────────────────────────────────────────────────────────

class SymbolQueues:
    """One bounded queue per symbol; on overflow, drop the oldest tick.

    Drop-oldest is intentional and counterintuitive: for a live-price system
    a tick from 400ms ago is strictly less useful than one from right now.
    The classic queue discipline (block/reject-newest) would preserve stale
    data at the cost of dropping fresh data — the opposite of what a price
    consumer wants.
    """

    def __init__(self, maxsize: int = 5) -> None:
        self._queues: dict[str, asyncio.Queue[ReconciledQuote]] = {}
        self.maxsize = maxsize

    def _q(self, symbol: str) -> asyncio.Queue[ReconciledQuote]:
        q = self._queues.get(symbol)
        if q is None:
            q = asyncio.Queue(maxsize=self.maxsize)
            self._queues[symbol] = q
        return q

    def enqueue(self, symbol: str, quote: ReconciledQuote) -> bool:
        """Returns True if an older tick was displaced to make room."""
        q = self._q(symbol)
        displaced = False
        if q.full():
            try:
                q.get_nowait()          # drop OLDEST — see docstring
                displaced = True
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(quote)
        except asyncio.QueueFull:
            # Vanishingly unlikely after a get_nowait; log and swallow.
            log.warning("SymbolQueues: still full after drop for %s", symbol)
        return displaced

    async def dequeue(self, symbol: str) -> ReconciledQuote:
        return await self._q(symbol).get()

    def depth(self, symbol: str) -> int:
        return self._q(symbol).qsize()


# ─────────────────────────────────────────────────────────────────────────────
# §09.2 — Per-symbol reconciliation rate cap
# ─────────────────────────────────────────────────────────────────────────────

class RateCap:
    """At most one reconciliation per symbol per interval.

    Bounds reconciler work regardless of ingest burst rate. Not a token
    bucket — a simple last-timestamp gate, which is enough because the only
    consumer is the reconciler itself, running in one worker.
    """

    def __init__(self, interval_s: float = 1.0) -> None:
        self.interval_s = interval_s
        self._last_at: dict[str, float] = {}

    def allow(self, symbol: str) -> bool:
        now = time.monotonic()
        last = self._last_at.get(symbol)
        if last is not None and now - last < self.interval_s:
            return False
        self._last_at[symbol] = now
        return True


# ─────────────────────────────────────────────────────────────────────────────
# §09.3 — Exchange-circuit awareness
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CircuitLimits:
    """NSE-published upper/lower circuit bounds for one symbol on one day.

    Populated from the exchange's own reference data (visible on Groww's own
    stock detail page — e.g. RELIANCE on 2026-09-04: lower ₹1,172.30,
    upper ₹1,432.70).
    """
    symbol: str
    upper: float
    lower: float
    as_of_date: str                    # ISO date; circuits reset each session

    # A price within `epsilon_pct` of either bound is treated as "in circuit".
    epsilon_pct: float = 0.001         # 0.1% band


def is_in_exchange_circuit(price: float, limits: CircuitLimits) -> bool:
    """Whether a live price is close enough to a circuit bound to suspend
    significance detection for the symbol."""
    if price <= 0:
        return False
    upper_gate = limits.upper * (1.0 - limits.epsilon_pct)
    lower_gate = limits.lower * (1.0 + limits.epsilon_pct)
    return price >= upper_gate or price <= lower_gate


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point the worker will call once per poll cycle per symbol.
# ─────────────────────────────────────────────────────────────────────────────

async def poll_symbol(
    symbol: str,
    reconciler: Reconciler,
    rate_cap: RateCap,
    queues: SymbolQueues,
) -> Optional[ReconciledQuote]:
    """One full reconciliation pass for one symbol.

    Returns None if the rate cap gated the call. Otherwise returns the
    reconciled quote (which may itself be UNCONFIRMED — the caller decides
    whether to act on it based on `quote.tier`).
    """
    if not rate_cap.allow(symbol):
        return None
    quote = await reconciler.reconcile(symbol)
    queues.enqueue(symbol, quote)
    return quote
