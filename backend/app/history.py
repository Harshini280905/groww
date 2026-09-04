"""Historical daily-bar fetcher (Yahoo Finance backend).

Not on the poll hot path — fetched once when a symbol is added to a
watchlist, then refreshed once per day (or on demand via the dev router).
Feeds the trailing-volatility calc in significance.py.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import TypedDict

import yfinance as yf


class DailyBarDict(TypedDict):
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


async def fetch_daily_bars(symbol: str, period: str = "60d") -> list[DailyBarDict]:
    """Returns bars oldest-first; empty list on any failure (caller handles)."""

    def _sync() -> list[DailyBarDict]:
        try:
            yf_sym = symbol if "." in symbol else f"{symbol}.NS"
            hist = yf.Ticker(yf_sym).history(period=period, auto_adjust=False)
            if hist is None or hist.empty:
                return []
            bars: list[DailyBarDict] = []
            for idx, row in hist.iterrows():
                # yfinance emits NaN closes for some sessions (holidays,
                # halts, gaps in its own data). Dropping them here keeps bad
                # bars out of the database entirely, rather than letting a
                # single NaN poison every volatility calculation downstream.
                try:
                    close = float(row["Close"])
                    open_ = float(row["Open"])
                    high = float(row["High"])
                    low = float(row["Low"])
                except (TypeError, ValueError):
                    continue
                if not all(math.isfinite(v) for v in (close, open_, high, low)):
                    continue
                if close <= 0:
                    continue
                try:
                    volume = float(row["Volume"])
                    if not math.isfinite(volume):
                        volume = 0.0
                except (TypeError, ValueError):
                    volume = 0.0
                bars.append(
                    {
                        "date": idx.to_pydatetime().replace(tzinfo=timezone.utc),
                        "open": open_, "high": high, "low": low,
                        "close": close, "volume": volume,
                    }
                )
            return bars
        except Exception:
            return []

    return await asyncio.to_thread(_sync)
