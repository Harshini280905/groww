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
import os

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
    # Mirrors pipeline.get_reconciler(): NSE is opt-in via ENABLE_NSE=1
    # because it 403s from datacenter IPs. Run `ENABLE_NSE=1 python smoke.py`
    # from a residential connection to exercise all three.
    enable_nse = os.getenv("ENABLE_NSE", "0") == "1"
    yahoo = YahooSource()
    bse = BSESource()
    nse = NSEDirectSource() if enable_nse else None
    sources = [yahoo, bse] if nse is None else [yahoo, nse, bse]
    print(f"sources: {', '.join(s.name for s in sources)}")
    reconciler = Reconciler(sources)

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
        if nse is not None:
            await nse.close()
        await bse.close()


if __name__ == "__main__":
    asyncio.run(main())
