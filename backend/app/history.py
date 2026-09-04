"""Historical daily-bar fetcher (Yahoo Finance backend).

Not on the poll hot path — fetched once when a symbol is added to a
watchlist, then refreshed once per day (or on demand via the dev router).
Feeds the trailing-volatility calc in significance.py.
"""

from __future__ import annotations

import asyncio
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
                bars.append(
                    {
                        "date": idx.to_pydatetime().replace(tzinfo=timezone.utc),
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row["Volume"]) if row["Volume"] else 0.0,
                    }
                )
            return bars
        except Exception:
            return []

    return await asyncio.to_thread(_sync)
