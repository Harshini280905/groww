"""BSE India adapter — the third, genuinely-independent source.

BSE (Bombay Stock Exchange) runs on separate infrastructure from NSE and
publishes its own quote data through api.bseindia.com. Adding it gives us:

  * A true 3-source configuration — one of Yahoo/NSE/BSE can fail without
    dropping the tier below VERIFIED.
  * Independence from NSE (different exchange, different systems, different
    circuit values) — so a genuine cross-verification of the ground-truth
    price, not a mirror of the same feed.
  * Less aggressive bot detection than NSE in practice — this adapter
    usually works out of the box with just TLS impersonation.

BSE uses NUMERIC SCRIP CODES rather than ticker symbols (RELIANCE = 500325,
TCS = 532540, etc.). A small hardcoded map covers the demo set; a production
build would seed the full mapping at startup from BSE's own listed-securities
endpoint. Symbols not in the map return `unknown_scrip_code` honestly rather
than falling silently.

Never raises. Every failure path returns a SourceReading with `error` set.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from curl_cffi.requests import AsyncSession

from ..market_data import CircuitBreaker, SourceReading


# Curated top-liquidity NSE-listed names → BSE scrip codes. Deliberately
# a small map for the demo; grow as needed. A production build would swap
# in a startup fetch from
# https://api.bseindia.com/BseIndiaAPI/api/ListofScripCodeSymbol/w?flag=Active
_SCRIP_CODES: dict[str, str] = {
    "RELIANCE":   "500325",
    "TCS":        "532540",
    "INFY":       "500209",
    "HDFCBANK":   "500180",
    "ICICIBANK":  "532174",
    "SBIN":       "500112",
    "ITC":        "500875",
    "BHARTIARTL": "532454",
    "LT":         "500510",
    "KOTAKBANK":  "500247",
    "HINDUNILVR": "500696",
    "AXISBANK":   "532215",
    "MARUTI":     "532500",
    "ASIANPAINT": "500820",
    "BAJFINANCE": "500034",
    "SUNPHARMA":  "524715",
    "NESTLEIND":  "500790",
    "WIPRO":      "507685",
    "ONGC":       "500312",
    "NTPC":       "532555",
    "POWERGRID":  "532898",
    "TATAMOTORS": "500570",
    "TATASTEEL":  "500470",
    "M&M":        "500520",
    "HCLTECH":    "532281",
    "TITAN":      "500114",
    "ULTRACEMCO": "532538",
    "COALINDIA":  "533278",
    "ADANIENT":   "512599",
    "JSWSTEEL":   "500228",
    "BAJAJFINSV": "532978",
    "TECHM":      "532755",
    "GRASIM":     "500300",
    "INDUSINDBK": "532187",
    "DRREDDY":    "500124",
    "CIPLA":      "500087",
    "EICHERMOT":  "505200",
    "HEROMOTOCO": "500182",
    "DIVISLAB":   "532488",
    "BPCL":       "500547",
}


BSE_HOME = "https://www.bseindia.com"
BSE_QUOTE_ENDPOINT = "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w"

# Match a currently-shipping Chrome. curl_cffi ships several impersonation
# profiles; chrome124 has the widest current-compatibility.
_IMPERSONATE = "chrome124"


def get_scrip_code(symbol: str) -> Optional[str]:
    """Public helper — check whether we can talk to BSE for a given symbol."""
    return _SCRIP_CODES.get(symbol.upper())


class BSESource:
    """Adapter for BSE India via its public JSON endpoint."""

    name = "bse"

    def __init__(self, timeout_s: float = 8.0) -> None:
        self.timeout_s = timeout_s
        self.breaker = CircuitBreaker(self.name)
        self._session: Optional[AsyncSession] = None
        self._session_warmed = False

    # -- session management --------------------------------------------------

    async def _ensure_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(impersonate=_IMPERSONATE)
            self._session_warmed = False
        if not self._session_warmed:
            # Single homepage GET is usually enough — BSE's bot detection is
            # less aggressive than NSE's.
            await self._session.get(BSE_HOME, timeout=self.timeout_s)
            self._session_warmed = True
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- fetch ---------------------------------------------------------------

    async def fetch(self, symbol: str) -> SourceReading:
        started = time.perf_counter()
        now = lambda: datetime.now(timezone.utc)
        latency = lambda: (time.perf_counter() - started) * 1000

        scrip = get_scrip_code(symbol)
        if scrip is None:
            return SourceReading(
                source=self.name, symbol=symbol,
                price=0.0, volume=None,
                fetched_at=now(), latency_ms=latency(),
                error="unknown_scrip_code",
            )

        try:
            session = await self._ensure_session()
        except Exception as e:
            return SourceReading(
                source=self.name, symbol=symbol,
                price=0.0, volume=None,
                fetched_at=now(), latency_ms=latency(),
                error=f"session_warm_failed: {type(e).__name__}",
            )

        # BSE's API-subdomain XHR checks Origin + Referer against bseindia.com
        api_headers = {
            "Origin": "https://www.bseindia.com",
            "Referer": "https://www.bseindia.com/",
        }
        try:
            r = await session.get(
                BSE_QUOTE_ENDPOINT,
                params={"Debtflag": "", "scripcode": scrip, "seriesid": ""},
                headers=api_headers,
                timeout=self.timeout_s,
            )
        except Exception as e:
            self._session_warmed = False
            return SourceReading(
                source=self.name, symbol=symbol,
                price=0.0, volume=None,
                fetched_at=now(), latency_ms=latency(),
                error=f"{type(e).__name__}: {str(e)[:100]}",
            )

        if r.status_code != 200:
            self._session_warmed = False
            return SourceReading(
                source=self.name, symbol=symbol,
                price=0.0, volume=None,
                fetched_at=now(), latency_ms=latency(),
                error=f"http_{r.status_code}",
            )

        try:
            data = r.json()
        except Exception as e:
            return SourceReading(
                source=self.name, symbol=symbol,
                price=0.0, volume=None,
                fetched_at=now(), latency_ms=latency(),
                error=f"json_decode: {type(e).__name__}",
            )

        # BSE response shape:
        #   {"CurrRate":{"LTP":"1327.60","PrevClose":"1302.50", ...}, ...}
        # LTP == last traded price. Volume keys vary by market phase.
        cur = data.get("CurrRate") or {}
        try:
            price = float(cur.get("LTP") or 0.0)
        except (ValueError, TypeError):
            price = 0.0

        volume: Optional[float] = None
        for k in ("TotalTradedQty", "TotalTradedQuantity", "Traded_Volume"):
            v = cur.get(k) or data.get(k)
            if v:
                try:
                    volume = float(v)
                    break
                except (ValueError, TypeError):
                    pass

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
