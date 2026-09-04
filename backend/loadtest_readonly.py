"""Controlled experiment: is the bottleneck the checkpoint WRITE?

/api/watchlist flatlined at ~47 rps with latency growing linearly in
concurrency — the signature of requests serializing behind one resource.
The obvious suspect is that this endpoint performs a WRITE on every read
(advancing each user's last_seen_at checkpoint), and SQLite serializes
writers.

This hammers /api/stocks/{symbol}/latest instead — same server, same
database, same auth-free FastAPI stack, but PURELY a read. If throughput
scales here and not there, the write is the culprit rather than the
framework, the DB round-trip count, or the machine.
"""

from __future__ import annotations

import asyncio
import statistics
import time

import httpx

BASE = "http://127.0.0.1:8801"
SYMBOL = "RELIANCE"


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(int(len(s) * p / 100), len(s) - 1)]


async def _hammer(client, n, lat, errs):
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            r = await client.get(f"{BASE}/api/stocks/{SYMBOL}/latest")
            dt = (time.perf_counter() - t0) * 1000
            (lat if r.status_code == 200 else errs).append(dt if r.status_code == 200 else r.status_code)
        except Exception as e:
            errs.append(type(e).__name__)


async def run(users: int, per_user: int) -> dict:
    limits = httpx.Limits(max_connections=users * 2, max_keepalive_connections=users * 2)
    async with httpx.AsyncClient(timeout=60.0, limits=limits) as client:
        lat, errs = [], []
        t0 = time.perf_counter()
        await asyncio.gather(*[_hammer(client, per_user, lat, errs) for _ in range(users)])
        wall = time.perf_counter() - t0
    total = len(lat) + len(errs)
    return {"users": users, "reqs": total, "rps": total / wall if wall else 0,
            "p50": _pct(lat, 50), "p95": _pct(lat, 95), "p99": _pct(lat, 99),
            "errors": len(errs)}


async def main():
    print(f"READ-ONLY endpoint: GET /api/stocks/{SYMBOL}/latest")
    print("Same server, same DB, no write on the request path.\n")
    print(f"{'users':>6} {'reqs':>7} {'rps':>9} {'p50 ms':>9} {'p95 ms':>9} {'p99 ms':>9} {'errors':>7}")
    print("-" * 62)
    for lvl in [1, 5, 10, 25, 50]:
        r = await run(lvl, 20)
        print(f"{r['users']:>6} {r['reqs']:>7} {r['rps']:>9.1f} "
              f"{r['p50']:>9.1f} {r['p95']:>9.1f} {r['p99']:>9.1f} {r['errors']:>7}")


if __name__ == "__main__":
    asyncio.run(main())
