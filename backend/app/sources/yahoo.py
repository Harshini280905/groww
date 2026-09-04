"""Yahoo Finance adapter — free, unofficial, no API key.

Uses the yfinance library because Yahoo maintains an internal session/crumb
dance for its endpoints that changes without notice; yfinance keeps up with
those changes so we don't have to. The library is synchronous, so we run
each fetch in a thread via asyncio.to_thread — that keeps the reconciler's
fan-out non-blocking without dragging in an aiohttp+cookie rewrite of what
yfinance already solves.

Known limitations (architecture blueprint §10):
  * India data can lag ~15 min behind live exchange prices.
  * Unofficial endpoint — no SLA. Aggressive polling can trigger rate limits.
  * Aggregated bar volume rather than trade-by-trade.

This adapter NEVER raises. Every failure path returns a SourceReading with
the `error` field populated — the reconciler relies on that contract so its
`asyncio.gather()` call cannot be poisoned by one broken source.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

from ..market_data import CircuitBreaker, SourceReading

try:
    import yfinance as yf
    _import_error: Optional[str] = None
except ImportError as e:              # pragma: no cover - import guard
    yf = None                          # type: ignore[assignment]
    _import_error = str(e)


class YahooSource:
    """Adapter for Yahoo Finance via yfinance."""

    name = "yahoo"

    def __init__(self, timeout_s: float = 10.0) -> None:
        if yf is None:
            raise RuntimeError(f"yfinance not installed: {_import_error}")
        self.timeout_s = timeout_s
        self.breaker = CircuitBreaker(self.name)

    # -- private -------------------------------------------------------------

    @staticmethod
    def _to_yahoo_symbol(symbol: str) -> str:
        """NSE symbols need a .NS suffix on Yahoo; caller may already supply one."""
        return symbol if "." in symbol else f"{symbol}.NS"

    def _fetch_sync(self, symbol: str) -> tuple[float, Optional[float]]:
        ticker = yf.Ticker(self._to_yahoo_symbol(symbol))
        # fast_info avoids the heavier .info request; last_price is what we need.
        info = ticker.fast_info
        price_raw = None
        for k in ("last_price", "lastPrice", "regular_market_price"):
            v = getattr(info, k, None)
            if v is None and hasattr(info, "__getitem__"):
                try: v = info[k]
                except (KeyError, TypeError): v = None
            if v:
                price_raw = v
                break
        price = float(price_raw) if price_raw else 0.0

        vol_raw = None
        for k in ("last_volume", "lastVolume", "regular_market_volume"):
            v = getattr(info, k, None)
            if v is None and hasattr(info, "__getitem__"):
                try: v = info[k]
                except (KeyError, TypeError): v = None
            if v:
                vol_raw = v
                break
        volume = float(vol_raw) if vol_raw else None
        return price, volume

    # -- public --------------------------------------------------------------

    async def fetch(self, symbol: str) -> SourceReading:
        started = time.perf_counter()
        now = lambda: datetime.now(timezone.utc)
        latency = lambda: (time.perf_counter() - started) * 1000

        try:
            price, volume = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_sync, symbol),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            return SourceReading(
                source=self.name, symbol=symbol,
                price=0.0, volume=None,
                fetched_at=now(), latency_ms=latency(),
                error="timeout",
            )
        except Exception as e:
            return SourceReading(
                source=self.name, symbol=symbol,
                price=0.0, volume=None,
                fetched_at=now(), latency_ms=latency(),
                error=f"{type(e).__name__}: {str(e)[:100]}",
            )

        if price <= 0:
            return SourceReading(
                source=self.name, symbol=symbol,
                price=0.0, volume=None,
                fetched_at=now(), latency_ms=latency(),
                error="empty_price",
            )
        return SourceReading(
            source=self.name, symbol=symbol,
            price=price, volume=volume,
            fetched_at=now(), latency_ms=latency(),
        )
