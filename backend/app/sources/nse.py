"""NSE India direct-JSON adapter — the "closest to official" free source.

NSE's site backend powers their public webpages, so the data path is short:
exchange → their CDN → us. It's undocumented (architecture blueprint §10
flags this honestly: "undocumented site backend, can break without notice").

WHY curl_cffi INSTEAD OF aiohttp:
  NSE's bot detection keys on the client's TLS handshake fingerprint. Plain
  aiohttp gives away that it's not a browser at the TLS layer and gets 401'd
  no matter how correct the headers and cookies look. curl_cffi wraps libcurl
  with browser TLS impersonation, so the handshake matches a real Chrome —
  which is what NSE's fingerprinting expects.

The adapter still NEVER raises. Every failure path returns a SourceReading
with `error` populated — the reconciler depends on that contract.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from curl_cffi.requests import AsyncSession

from ..market_data import CircuitBreaker, SourceReading


NSE_HOME = "https://www.nseindia.com"
# Warming against a stock quote page after the homepage gives NSE's detector
# a more browser-shaped session (multiple resource requests + realistic
# navigation pattern) than a bare homepage hit.
NSE_WARM_URL = "https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE"
NSE_QUOTE_ENDPOINT = "https://www.nseindia.com/api/quote-equity"

# Match a currently-shipping Chrome. curl_cffi ships several impersonation
# profiles — chrome124 is recent enough to still pass most detectors.
_IMPERSONATE = "chrome124"


class NSEDirectSource:
    """Adapter for NSE India's public JSON endpoint via TLS-impersonating client."""

    name = "nse"

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
            # Two-step warm: homepage → quote page. Both populate the
            # cookies NSE's /api/ handlers check for.
            await self._session.get(NSE_HOME, timeout=self.timeout_s)
            await self._session.get(NSE_WARM_URL, timeout=self.timeout_s)
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

        try:
            session = await self._ensure_session()
        except Exception as e:
            return SourceReading(
                source=self.name, symbol=symbol,
                price=0.0, volume=None,
                fetched_at=now(), latency_ms=latency(),
                error=f"session_warm_failed: {type(e).__name__}",
            )

        # These headers match what a real browser sends on the XHR call the
        # NSE frontend makes to this endpoint — curl_cffi handles TLS but
        # NSE also inspects request-shape headers on /api/ specifically.
        xhr_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={symbol.upper()}",
        }
        try:
            r = await session.get(
                NSE_QUOTE_ENDPOINT,
                params={"symbol": symbol.upper()},
                headers=xhr_headers,
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
            self._session_warmed = False        # force re-warm next call
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

        price_info = data.get("priceInfo") or {}
        price = float(price_info.get("lastPrice") or 0.0)

        # NSE's payload places session-cumulative volume under a couple of
        # different keys depending on market phase; try both.
        volume = None
        for path in (
            ("preOpenMarket", "totalTradedVolume"),
            ("securityWiseDP", "quantityTraded"),
        ):
            node = data
            for k in path:
                node = node.get(k) if isinstance(node, dict) else None
            if node:
                try: volume = float(node); break
                except (TypeError, ValueError): pass

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
