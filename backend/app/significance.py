"""Statistical significance detector.

Given a ReconciledQuote and recent daily bar history, decides whether the
current price constitutes a "meaningfully changed" move worth surfacing.

Design references (architecture blueprint):
  §06.2 — Volatility-normalized significance: z-score against the stock's
          OWN trailing volatility, not a fixed percentage. A 2% move on
          a bluechip is different from a 2% move on a small-cap; the math
          reflects that automatically.
  §06.3 — Tier-gated significance. VERIFIED = normal threshold; a
          BEST_AVAILABLE quote requires a stronger signal (we trust the
          number less, so we require more evidence before crying wolf);
          an UNCONFIRMED quote never fires an event.
  §09.3 — Exchange-circuit awareness. While a symbol is in circuit, any
          "move" is regulatory-defined, not a market signal — flagging it
          as significant would be a lie. Detection is suspended.
  §11   — Detection is fully deterministic. No LLM decides whether a move
          is real; that stays auditable, reproducible, and cheap to run
          per tick across every watched symbol.

This module is PURE — no I/O, no DB, no globals. Every input is a parameter;
every output is a return value. Callers wire in persistence themselves.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from .market_data import ConfidenceTier, ReconciledQuote


# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

# Below this many historical bars, a trailing volatility estimate has too
# much sampling error to be useful — we bail out with INSUFFICIENT_HISTORY
# rather than emit a shaky z-score.
MIN_HISTORY_BARS = 5
VOLATILITY_WINDOW = 20

# Fallback rule when trailing volatility is exactly zero (a stock that hasn't
# moved at all in the window). Any move past this % triggers on abs return.
FLAT_HISTORY_FALLBACK_PCT = 0.05  # 5%


@dataclass(frozen=True)
class Thresholds:
    """Two ways a move can trigger:
      z_solo      — |z| alone is large enough
      z_combined  — |z| is moderate AND volume is unusually high
    """
    z_solo: float
    z_combined: float
    volume_ratio: float


# Tier-gated thresholds — the mechanism that makes multi-source confidence
# actually control what the user sees.
#
#   VERIFIED       — cross-verified across ≥2 sources. Standard bar: 2σ ≈
#                    tail on either side, ≈ 5% of daily returns.
#   BEST_AVAILABLE — only one source responded. We can't rule out a
#                    single-source glitch, so we require a stronger signal
#                    before firing. 3σ ≈ 0.3% of daily returns.
#   UNCONFIRMED    — never fires. Full stop.
VERIFIED_THRESHOLDS = Thresholds(z_solo=2.0, z_combined=1.2, volume_ratio=2.0)
BEST_AVAILABLE_THRESHOLDS = Thresholds(z_solo=3.0, z_combined=2.0, volume_ratio=3.0)


class SignificanceReason(str, Enum):
    """Every SignificanceResult carries one of these — the reason is part of
    the audit trail. A judge asking "why didn't this fire?" gets a real
    answer, not a shrug."""
    SIGNIFICANT_Z = "significant_z"
    SIGNIFICANT_Z_VOLUME = "significant_z_and_volume"
    SIGNIFICANT_FLAT_FALLBACK = "significant_flat_fallback"
    BELOW_THRESHOLD = "below_threshold"
    INSUFFICIENT_HISTORY = "insufficient_history"
    UNCONFIRMED_QUOTE = "unconfirmed_quote"
    IN_EXCHANGE_CIRCUIT = "in_exchange_circuit"
    ZERO_VOLATILITY_NO_MOVE = "zero_volatility_no_move"


@dataclass(frozen=True)
class SignificantEvent:
    """The payload the persistence layer writes into `significant_events`."""
    symbol: str
    ts: datetime
    z_score: float
    volume_ratio: float
    direction: str            # "up" | "down"
    price_before: float
    price_after: float
    confidence: float
    note: str


@dataclass(frozen=True)
class SignificanceResult:
    """The verdict for one detection pass. Never None — every call returns
    a result with a `reason`. `event` is populated only when significant."""
    symbol: str
    is_significant: bool
    reason: SignificanceReason
    z_score: Optional[float] = None
    volume_ratio: Optional[float] = None
    todays_return_pct: Optional[float] = None
    event: Optional[SignificantEvent] = None


# ─────────────────────────────────────────────────────────────────────────────
# Pure math — testable in isolation
# ─────────────────────────────────────────────────────────────────────────────

def trailing_volatility(
    daily_closes: list[float], window: int = VOLATILITY_WINDOW
) -> tuple[float, float]:
    """Returns (mean_return, stddev_return) over the trailing `window` bars.

    Computes daily returns internally from consecutive closes — caller
    passes prices, not returns.
    """
    if len(daily_closes) < 2:
        return 0.0, 0.0
    closes = daily_closes[-(window + 1):] if len(daily_closes) > window else daily_closes
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    if not returns:
        return 0.0, 0.0
    mean_r = statistics.mean(returns)
    stddev_r = statistics.stdev(returns) if len(returns) > 1 else 0.0
    return mean_r, stddev_r


def compute_z_score(
    current_price: float, prev_close: float,
    mean_return: float, stddev_return: float,
) -> Optional[float]:
    if prev_close <= 0 or stddev_return == 0.0:
        return None
    todays_return = (current_price - prev_close) / prev_close
    return (todays_return - mean_return) / stddev_return


def compute_volume_ratio(
    current_volume: Optional[float], avg_volume: Optional[float]
) -> float:
    """Ratio of today's volume to trailing average. Defaults to 1.0 when
    volume data is missing — the combined-trigger path is opt-in via a
    threshold check, so 1.0 (== "normal") means it won't fire on missing data.
    """
    if current_volume is None or avg_volume is None or avg_volume <= 0:
        return 1.0
    return current_volume / avg_volume


def _thresholds_for(tier: ConfidenceTier) -> Optional[Thresholds]:
    if tier is ConfidenceTier.VERIFIED:
        return VERIFIED_THRESHOLDS
    if tier is ConfidenceTier.BEST_AVAILABLE:
        return BEST_AVAILABLE_THRESHOLDS
    return None


def _direction(price_after: float, price_before: float) -> str:
    return "up" if price_after >= price_before else "down"


def _build_event(
    quote: ReconciledQuote, prev_close: float,
    z_score: Optional[float], volume_ratio: float, note: str,
) -> SignificantEvent:
    return SignificantEvent(
        symbol=quote.symbol,
        ts=quote.resolved_at,
        z_score=z_score if z_score is not None else 0.0,
        volume_ratio=volume_ratio,
        direction=_direction(quote.price, prev_close),
        price_before=prev_close,
        price_after=quote.price,
        confidence=quote.confidence,
        note=note,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def detect_significance(
    quote: ReconciledQuote,
    daily_closes: list[float],
    daily_volumes: Optional[list[float]] = None,
    prev_close: Optional[float] = None,
    is_in_circuit: bool = False,
) -> SignificanceResult:
    """Decide whether `quote` is a significant move.

    Args:
        quote: The reconciled current price (carries confidence tier).
        daily_closes: Historical closing prices, oldest first, most recent
            last. The most recent element is used as prev_close if none is
            provided explicitly.
        daily_volumes: Historical volumes, parallel to daily_closes. Optional;
            the combined z+volume trigger is only usable when this is given.
        prev_close: Explicit previous close. Falls back to daily_closes[-1].
        is_in_circuit: Whether the exchange has this symbol in circuit right
            now (upper/lower price bound hit). See §9.3.

    Returns:
        SignificanceResult with `reason` explaining the verdict. Pure — no
        side effects, no I/O.
    """
    # Circuit gate (§9.3) — earliest, cheapest, most important.
    if is_in_circuit:
        return SignificanceResult(
            symbol=quote.symbol,
            is_significant=False,
            reason=SignificanceReason.IN_EXCHANGE_CIRCUIT,
        )

    # Confidence gate (§06.3) — an UNCONFIRMED quote never triggers.
    if quote.tier is ConfidenceTier.UNCONFIRMED:
        return SignificanceResult(
            symbol=quote.symbol,
            is_significant=False,
            reason=SignificanceReason.UNCONFIRMED_QUOTE,
        )

    thresholds = _thresholds_for(quote.tier)
    if thresholds is None:
        return SignificanceResult(
            symbol=quote.symbol, is_significant=False,
            reason=SignificanceReason.UNCONFIRMED_QUOTE,
        )

    # Cold-start guard — a z-score against < 5 bars is not a claim we can defend.
    if len(daily_closes) < MIN_HISTORY_BARS:
        return SignificanceResult(
            symbol=quote.symbol,
            is_significant=False,
            reason=SignificanceReason.INSUFFICIENT_HISTORY,
        )

    prev_close_used = prev_close if prev_close is not None else daily_closes[-1]
    mean_r, stddev_r = trailing_volatility(daily_closes)
    todays_return_pct = (
        ((quote.price - prev_close_used) / prev_close_used) * 100
        if prev_close_used > 0 else 0.0
    )

    # Zero-volatility fallback — a stock that hasn't moved at all suddenly
    # moving is significant, but our z-score math divides by zero. Trigger
    # only on a large absolute move to avoid false positives on rounding.
    if stddev_r == 0.0:
        if prev_close_used > 0:
            move_pct = abs(quote.price - prev_close_used) / prev_close_used
            if move_pct >= FLAT_HISTORY_FALLBACK_PCT:
                event = _build_event(
                    quote, prev_close_used, z_score=None, volume_ratio=1.0,
                    note=f"flat-history fallback: {todays_return_pct:+.2f}%",
                )
                return SignificanceResult(
                    symbol=quote.symbol, is_significant=True,
                    reason=SignificanceReason.SIGNIFICANT_FLAT_FALLBACK,
                    z_score=None, volume_ratio=1.0,
                    todays_return_pct=todays_return_pct, event=event,
                )
        return SignificanceResult(
            symbol=quote.symbol, is_significant=False,
            reason=SignificanceReason.ZERO_VOLATILITY_NO_MOVE,
            todays_return_pct=todays_return_pct,
        )

    z = compute_z_score(quote.price, prev_close_used, mean_r, stddev_r)
    if z is None:
        return SignificanceResult(
            symbol=quote.symbol, is_significant=False,
            reason=SignificanceReason.INSUFFICIENT_HISTORY,
        )

    # Volume ratio (optional — only usable if we have historical volumes)
    avg_vol: Optional[float] = None
    if daily_volumes and len(daily_volumes) >= MIN_HISTORY_BARS:
        recent = [v for v in daily_volumes[-VOLATILITY_WINDOW:] if v and v > 0]
        if recent:
            avg_vol = statistics.mean(recent)
    vol_ratio = compute_volume_ratio(quote.volume, avg_vol)

    abs_z = abs(z)
    fired: Optional[SignificanceReason] = None
    if abs_z >= thresholds.z_solo:
        fired = SignificanceReason.SIGNIFICANT_Z
    elif abs_z >= thresholds.z_combined and vol_ratio >= thresholds.volume_ratio:
        fired = SignificanceReason.SIGNIFICANT_Z_VOLUME

    if fired is None:
        return SignificanceResult(
            symbol=quote.symbol, is_significant=False,
            reason=SignificanceReason.BELOW_THRESHOLD,
            z_score=z, volume_ratio=vol_ratio,
            todays_return_pct=todays_return_pct,
        )

    event = _build_event(
        quote, prev_close_used, z_score=z, volume_ratio=vol_ratio,
        note=f"{fired.value}: z={z:+.2f} vol_ratio={vol_ratio:.2f} tier={quote.tier.value}",
    )
    return SignificanceResult(
        symbol=quote.symbol, is_significant=True, reason=fired,
        z_score=z, volume_ratio=vol_ratio,
        todays_return_pct=todays_return_pct, event=event,
    )
