"""Proof that significance detection actually fires on real market data.

Runs the REAL detector (app.significance.detect_significance — the exact
function the live pipeline calls, not a copy) against real historical daily
bars, replaying each day as if it were "today". Prints which days would have
produced a SignificantEvent and why.

The point: "Nothing unusual" is a real verdict computed from real numbers,
not a placeholder that never changes.

    python prove_detection.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.history import fetch_daily_bars
from app.market_data import ConfidenceTier, ReconciledQuote
from app.significance import VOLATILITY_WINDOW, detect_significance, trailing_volatility

SYMBOLS = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "TATAMOTORS"]


def _quote(symbol: str, price: float, volume: float) -> ReconciledQuote:
    """A VERIFIED-tier quote, so we exercise the normal (strictest-usable)
    threshold path rather than the raised BEST_AVAILABLE one."""
    return ReconciledQuote(
        symbol=symbol, price=price, volume=volume,
        resolved_at=datetime.now(timezone.utc),
        confidence=0.95, tier=ConfidenceTier.VERIFIED,
        coverage=1.0, agreement=1.0, freshness=1.0, readings=(),
    )


async def main() -> None:
    print("Replaying real market history through the live significance detector.\n")
    grand_fired = grand_days = 0

    for symbol in SYMBOLS:
        bars = await fetch_daily_bars(symbol, period="6mo")
        if len(bars) < VOLATILITY_WINDOW + 5:
            print(f"{symbol}: not enough history\n")
            continue

        closes = [b["close"] for b in bars]
        volumes = [b["volume"] for b in bars]

        fired: list[tuple[str, float, float, str]] = []
        checked = 0

        # Walk forward: for each day, the detector only ever sees days BEFORE
        # it — no lookahead, exactly as it works live.
        for i in range(VOLATILITY_WINDOW + 1, len(bars)):
            hist_closes = closes[:i]
            hist_vols = volumes[:i]
            today_close = closes[i]
            today_vol = volumes[i]
            checked += 1

            result = detect_significance(
                _quote(symbol, today_close, today_vol),
                daily_closes=hist_closes,
                daily_volumes=hist_vols,
            )
            if result.is_significant:
                fired.append((
                    bars[i]["date"].strftime("%Y-%m-%d"),
                    result.todays_return_pct or 0.0,
                    result.z_score or 0.0,
                    result.reason.value,
                ))

        _, sigma = trailing_volatility(closes)
        grand_fired += len(fired)
        grand_days += checked

        print(f"-- {symbol} " + "-" * (58 - len(symbol)))
        print(f"   normal daily move (sigma): {sigma*100:.2f}%")
        print(f"   days replayed: {checked}   flagged unusual: {len(fired)}"
              f"   ({len(fired)/checked*100:.1f}%)")
        for date, ret, z, reason in fired[-6:]:
            print(f"     {date}  {ret:+6.2f}%   z={z:+5.2f}"
                  f"  ({abs(z):.1f}x normal)  {reason}")
        if not fired:
            print("     (no day in this window crossed the threshold)")
        print()

    print("=" * 66)
    print(f"TOTAL: {grand_fired} events fired across {grand_days} replayed days "
          f"({grand_fired/max(grand_days,1)*100:.1f}%)")
    print("A ~2-6% hit rate is the design working: |z| >= 2 is meant to be rare.")
    print("On the other ~95% of days the honest answer really is 'nothing unusual'.")


if __name__ == "__main__":
    asyncio.run(main())
