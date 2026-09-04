"""End-to-end live smoke test.

DELIBERATELY hits the real internet — Yahoo Finance + NSE India — so we can
see with our own eyes that the pipeline works with real data, not just stubs.
Not a unit test; run manually:

    python smoke.py

Prints, per symbol:
  * Each source's raw reading (price, latency, error if any)
  * The resolved quote from the reconciler
  * Confidence tier and its three decomposed terms

Because it hits the network, results vary — outside market hours you still
get the previous close; if NSE cookies fail you'll see only Yahoo respond
and the confidence tier will drop, which is exactly the behavior the design
was built for. That's the point of running it: verify the honest failure
modes show up as designed.
"""

from __future__ import annotations

import asyncio

from app.market_data import Reconciler
from app.sources.bse import BSESource
from app.sources.nse import NSEDirectSource
from app.sources.yahoo import YahooSource


TIER_BADGE = {
    "verified": "[VERIFIED]",
    "best_available": "[BEST-AVAILABLE]",
    "unconfirmed": "[UNCONFIRMED]",
}


async def main():
    yahoo = YahooSource()
    nse = NSEDirectSource()
    bse = BSESource()
    reconciler = Reconciler([yahoo, nse, bse])

    try:
        for symbol in ["RELIANCE", "TCS", "INFY"]:
            print(f"\n--- {symbol} " + "-" * (55 - len(symbol)))
            quote = await reconciler.reconcile(symbol)
            for r in quote.readings:
                status = "OK " if r.ok else "ERR"
                price_s = f"Rs {r.price:>10,.2f}" if r.ok else "      -      "
                err_s = f"  err={r.error}" if r.error else ""
                print(f"  [{status}] {r.source:8}  {price_s}  {r.latency_ms:>6.0f}ms{err_s}")
            tier = TIER_BADGE.get(quote.tier.value, quote.tier.value)
            print(f"  -> resolved Rs {quote.price:,.2f}  |  confidence {quote.confidence:.2f}  |  {tier}")
            print(f"     coverage={quote.coverage:.2f}  agreement={quote.agreement:.2f}  freshness={quote.freshness:.2f}")
    finally:
        await nse.close()
        await bse.close()


if __name__ == "__main__":
    asyncio.run(main())
