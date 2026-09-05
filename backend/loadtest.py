"""Load test for the read path — the one that scales with users.

METHODOLOGY, stated up front so the numbers can be judged honestly:

  * Targets a LOCAL server, not the Render free tier. Load-testing a 512MB
    shared instance measures Render's plan limits, not this application.
  * External sources (Yahoo/BSE) are never called. The database is seeded
    directly, so what's measured is THIS system's read path — not yfinance's
    latency or BSE's rate limiter. Mixing those in would make the numbers
    meaningless.
  * Measures GET /api/watchlist, which is the endpoint whose cost actually
    grows with user count. Ingestion deliberately does not: it's bounded by
    distinct symbols watched (see pipeline.distinct_watched_symbols).
  * Each virtual user has their own JWT and their own watchlist rows, so
    per-user work (the last_seen_at checkpoint write) is genuinely
    exercised rather than sharing one hot row.

Run:
    python loadtest.py                # default sweep
    python loadtest.py --users 50 --requests 400
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx

BASE = "http://127.0.0.1:8877"


async def _register(client: httpx.AsyncClient, i: int) -> str | None:
    """Each virtual user is a real account with a real JWT."""
    email = f"load{i}@example.com"
    r = await client.post(f"{BASE}/api/auth/register",
                          json={"email": email, "password": "loadtest123"})
    if r.status_code == 409:
        r = await client.post(f"{BASE}/api/auth/login",
                              json={"email": email, "password": "loadtest123"})
    if r.status_code != 200:
        return None
    return r.json()["access_token"]


async def _seed_watchlist(client: httpx.AsyncClient, token: str, symbols: list[str]) -> None:
    h = {"Authorization": f"Bearer {token}"}
    for s in symbols:
        await client.post(f"{BASE}/api/watchlist", json={"symbol": s, "intent_tag": None}, headers=h)


async def _hammer(client: httpx.AsyncClient, token: str, n: int,
                  lat: list[float], errs: list[str]) -> None:
    h = {"Authorization": f"Bearer {token}"}
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            r = await client.get(f"{BASE}/api/watchlist", headers=h)
            dt = (time.perf_counter() - t0) * 1000
            if r.status_code == 200:
                lat.append(dt)
            else:
                errs.append(f"HTTP {r.status_code}")
        except Exception as e:
            errs.append(type(e).__name__)


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = min(int(len(s) * p / 100), len(s) - 1)
    return s[k]


async def provision(max_users: int, symbols: list[str]) -> list[str]:
    """Create accounts + watchlists ONCE, before any measurement.

    Registration is deliberately throttled in small batches: pbkdf2_sha256
    is intentionally slow (that's what a password hash is for), and FastAPI
    runs sync endpoints on a bounded threadpool, so firing 100 concurrent
    signups saturates it and times out. That's a real property of the
    signup path — but it is NOT what this test measures, so it must not
    contaminate the read-path numbers.
    """
    # Fast path: if the accounts already exist from a previous run, mint
    # their tokens locally instead of paying for 100 pbkdf2 logins. Signing
    # a JWT is the same operation the login endpoint performs after the
    # password check — we're skipping the deliberately-slow hash, not
    # bypassing auth. The tokens are ordinary and fully validated server-side.
    try:
        from app.auth import create_access_token
        from app.db import SessionLocal
        from app.models import User, WatchlistItem
        db = SessionLocal()
        existing = (
            db.query(User)
            .filter(User.email.like("load%@example.com"))
            .order_by(User.id)
            .limit(max_users)
            .all()
        )
        have_lists = {
            uid for (uid,) in db.query(WatchlistItem.user_id).distinct().all()
        }
        ready = [u for u in existing if u.id in have_lists]
        db.close()
        if len(ready) >= max_users:
            print(f"  reusing {max_users} pre-provisioned accounts")
            return [create_access_token(u.id) for u in ready[:max_users]]
    except Exception as e:
        print(f"  (could not reuse accounts: {e}; provisioning fresh)")

    tokens: list[str] = []
    async with httpx.AsyncClient(timeout=180.0) as client:
        BATCH = 5
        for start in range(0, max_users, BATCH):
            got = await asyncio.gather(
                *[_register(client, i) for i in range(start, min(start + BATCH, max_users))]
            )
            tokens.extend([t for t in got if t])
        for start in range(0, len(tokens), BATCH):
            await asyncio.gather(
                *[_seed_watchlist(client, t, symbols) for t in tokens[start:start + BATCH]]
            )
    if not tokens:
        raise RuntimeError("could not create any users")
    return tokens


async def run_level(tokens: list[str], per_user: int) -> dict:
    """Measure ONLY the read path, using pre-provisioned accounts."""
    users = len(tokens)
    limits = httpx.Limits(max_connections=users * 2, max_keepalive_connections=users * 2)
    async with httpx.AsyncClient(timeout=60.0, limits=limits) as client:
        lat: list[float] = []
        errs: list[str] = []

        t0 = time.perf_counter()
        await asyncio.gather(*[_hammer(client, t, per_user, lat, errs) for t in tokens])
        wall = time.perf_counter() - t0

    total = len(lat) + len(errs)
    return {
        "users": users,
        "requests": total,
        "ok": len(lat),
        "errors": len(errs),
        "err_kinds": {k: errs.count(k) for k in set(errs)},
        "wall_s": wall,
        "rps": total / wall if wall else 0,
        "p50": _pct(lat, 50), "p95": _pct(lat, 95), "p99": _pct(lat, 99),
        "mean": statistics.mean(lat) if lat else 0,
        "max": max(lat) if lat else 0,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=0, help="single level instead of a sweep")
    ap.add_argument("--requests", type=int, default=20, help="requests per virtual user")
    ap.add_argument("--symbols", type=int, default=5)
    args = ap.parse_args()

    all_syms = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN", "ITC", "WIPRO", "LT"]
    symbols = all_syms[:args.symbols]

    levels = [args.users] if args.users else [1, 5, 10, 25, 50, 100]

    print(f"Target      : {BASE}/api/watchlist")
    print(f"Watchlist   : {len(symbols)} symbols per user")
    print(f"Requests    : {args.requests} per virtual user")
    print("External market APIs are NOT called - measuring this system only.")
    max_users = max(levels)
    print(f"\nProvisioning {max_users} accounts (setup, not measured)...")
    all_tokens = await provision(max_users, symbols)
    print(f"ready: {len(all_tokens)} accounts\n")

    print(f"{'users':>6} {'reqs':>7} {'rps':>9} {'p50 ms':>9} {'p95 ms':>9} {'p99 ms':>9} {'max ms':>9} {'errors':>7}")
    print("-" * 76)

    for lvl in levels:
        r = await run_level(all_tokens[:lvl], args.requests)
        print(f"{r['users']:>6} {r['requests']:>7} {r['rps']:>9.1f} "
              f"{r['p50']:>9.1f} {r['p95']:>9.1f} {r['p99']:>9.1f} {r['max']:>9.1f} "
              f"{r['errors']:>7}")
        if r["err_kinds"]:
            print(f"        errors: {r['err_kinds']}")

    print("\nNote: GET /api/watchlist also WRITES (it advances each user's")
    print("last_seen_at checkpoint), so these numbers include a write per")
    print("request — this is not a read-only benchmark.")


if __name__ == "__main__":
    asyncio.run(main())
