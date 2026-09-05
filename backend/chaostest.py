"""Chaos testing — fault injection against the real reconciliation pipeline.

Unit tests check that each piece behaves when used correctly. This checks
what happens when the world misbehaves: sources dying, hanging, lying,
returning garbage, or recovering. It drives the REAL Reconciler and the
REAL significance detector with deliberately broken adapters — no mocks of
our own logic, only faults injected at the boundary where reality is
actually unreliable.

The invariant every scenario is really testing is one sentence:

    the system must NEVER present a fabricated or unverified price as fact.

Degrading honestly is a pass. Crashing is a fail. Silently inventing a
number is the worst possible failure and is what most of these scenarios
are hunting for.

    python chaostest.py
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from app.market_data import (
    CircuitBreaker, CircuitState, ConfidenceTier, Reconciler, SourceReading,
)
from app.significance import detect_significance

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}\n         {detail}")


# ── fault-injecting adapters ────────────────────────────────────────────────

class DeadSource:
    """Always errors, as a real adapter must (returns, never raises)."""
    def __init__(self, name="dead", err="conn_refused"):
        self.name, self._err = name, err
        self.breaker = CircuitBreaker(name)

    async def fetch(self, symbol):
        return SourceReading(source=self.name, symbol=symbol, price=0.0, volume=None,
                             fetched_at=datetime.now(timezone.utc),
                             latency_ms=1.0, error=self._err)


class RaisingSource:
    """Violates the adapter contract by raising — the reconciler must contain it."""
    def __init__(self, name="raiser"):
        self.name = name
        self.breaker = CircuitBreaker(name)

    async def fetch(self, symbol):
        raise RuntimeError("adapter exploded")


class HangingSource:
    """Never returns. Tests that one dead source can't stall everything."""
    def __init__(self, name="hang", delay=30.0):
        self.name, self.delay = name, delay
        self.breaker = CircuitBreaker(name)

    async def fetch(self, symbol):
        await asyncio.sleep(self.delay)
        return SourceReading(source=self.name, symbol=symbol, price=1.0, volume=None,
                             fetched_at=datetime.now(timezone.utc), latency_ms=0.0)


class LyingSource:
    """Returns a confidently wrong price — the dangerous failure mode."""
    def __init__(self, name, price):
        self.name, self.price = name, price
        self.breaker = CircuitBreaker(name)

    async def fetch(self, symbol):
        return SourceReading(source=self.name, symbol=symbol, price=self.price,
                             volume=1000.0, fetched_at=datetime.now(timezone.utc),
                             latency_ms=5.0)


class StaleSource:
    """Healthy-looking but its data is hours old."""
    def __init__(self, name, price, age_s):
        self.name, self.price, self.age = name, price, age_s
        self.breaker = CircuitBreaker(name)

    async def fetch(self, symbol):
        return SourceReading(source=self.name, symbol=symbol, price=self.price, volume=1000.0,
                             fetched_at=datetime.now(timezone.utc) - timedelta(seconds=self.age),
                             latency_ms=5.0)


class FlakySource:
    """Fails N times then recovers — exercises breaker open → half-open → closed."""
    def __init__(self, name, fail_times, price=100.0):
        self.name, self.remaining, self.price = name, fail_times, price
        self.breaker = CircuitBreaker(name, failure_threshold=3, base_cooldown_s=0.05)
        self.calls = 0

    async def fetch(self, symbol):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return SourceReading(source=self.name, symbol=symbol, price=0.0, volume=None,
                                 fetched_at=datetime.now(timezone.utc),
                                 latency_ms=1.0, error="flaky_failure")
        return SourceReading(source=self.name, symbol=symbol, price=self.price, volume=1000.0,
                             fetched_at=datetime.now(timezone.utc), latency_ms=5.0)


HEALTHY = lambda n, p=100.0: LyingSource(n, p)   # a "lying" source telling the truth


# ── scenarios ───────────────────────────────────────────────────────────────

async def s1_total_blackout():
    print("\n1. TOTAL BLACKOUT — every source down")
    q = await Reconciler([DeadSource("a"), DeadSource("b"), DeadSource("c")]).reconcile("TCS")
    check("no price is invented", q.price == 0.0, f"price={q.price}, confidence={q.confidence:.2f}")
    check("tier is UNCONFIRMED", q.tier is ConfidenceTier.UNCONFIRMED, f"tier={q.tier.value}")
    r = detect_significance(q, daily_closes=[100.0] * 25)
    check("no event can fire on dead data", not r.is_significant, f"reason={r.reason.value}")


async def s2_single_survivor():
    print("\n2. SINGLE SURVIVOR — only one source answers")
    q = await Reconciler([HEALTHY("ok", 100.0), DeadSource("b"), DeadSource("c")]).reconcile("TCS")
    check("price is served (not withheld)", q.price == 100.0, f"price={q.price}")
    check("but NEVER labelled verified", q.tier is not ConfidenceTier.VERIFIED,
          f"tier={q.tier.value} — one source cannot cross-verify itself")


async def s3_adapter_raises():
    print("\n3. CONTRACT VIOLATION — an adapter raises instead of returning")
    q = await Reconciler([RaisingSource("boom"), HEALTHY("ok", 100.0), HEALTHY("ok2", 100.2)]).reconcile("TCS")
    check("one bad adapter cannot poison the batch", q.price > 0,
          f"survivors still resolved price={q.price:.2f}")
    check("failure recorded, not swallowed silently",
          any(r.error for r in q.readings), "error present in readings")


async def s4_hanging_source():
    print("\n4. HANGING SOURCE — one never responds")
    hang = HangingSource("hang", delay=60.0)
    t0 = time.perf_counter()
    q = await asyncio.wait_for(
        Reconciler([hang, HEALTHY("ok", 100.0), HEALTHY("ok2", 100.1)]).reconcile("TCS"),
        timeout=20.0,
    )
    dt = time.perf_counter() - t0
    check("healthy sources are not blocked by a hung one", q.price > 0 and dt < 16,
          f"resolved {q.price:.2f} in {dt:.1f}s (hung source would have taken 60s)")


async def s5_outlier_liar():
    print("\n5. CONFIDENT LIAR — one source reports a wildly wrong price")
    # Two agree near 100; one insists on 5000.
    q = await Reconciler([HEALTHY("a", 100.0), HEALTHY("b", 100.4), LyingSource("liar", 5000.0)]).reconcile("TCS")
    dragged = abs(q.price - 100.2) > 5
    check("median resists the outlier", not dragged,
          f"resolved {q.price:.2f} (mean would be ~{(100.0+100.4+5000)/3:.0f})")
    check("disagreement is reflected in confidence", q.agreement < 0.5,
          f"agreement={q.agreement:.2f} — spread was detected, not hidden")


async def s6_stale_data():
    print("\n6. STALE DATA — sources answer, but with old prices")
    q = await Reconciler([StaleSource("a", 100.0, 600), StaleSource("b", 100.1, 600)]).reconcile("TCS")
    check("staleness collapses freshness", q.freshness == 0.0, f"freshness={q.freshness:.2f}")
    check("stale data cannot reach VERIFIED", q.tier is not ConfidenceTier.VERIFIED,
          f"tier={q.tier.value}, confidence={q.confidence:.2f}")


async def s7_breaker_opens():
    print("\n7. CIRCUIT BREAKER — repeated failures stop the hammering")
    flaky = FlakySource("flaky", fail_times=99)
    rec = Reconciler([flaky, HEALTHY("ok", 100.0)])
    for _ in range(4):
        await rec.reconcile("TCS")
    opened = flaky.breaker.state is CircuitState.OPEN
    calls_before = flaky.calls
    await rec.reconcile("TCS")
    check("breaker opens after repeated failures", opened, f"state={flaky.breaker.state.value}")
    check("open breaker stops calling the dead source",
          flaky.calls == calls_before, f"calls held at {flaky.calls} — no new request issued")


async def s8_breaker_recovers():
    print("\n8. RECOVERY — source heals, breaker must let it back in")
    flaky = FlakySource("flaky", fail_times=3, price=100.0)
    rec = Reconciler([flaky, HEALTHY("ok", 100.0)])
    for _ in range(4):
        await rec.reconcile("TCS")
    await asyncio.sleep(0.3)                       # let cooldown elapse
    q = await rec.reconcile("TCS")
    healed = any(r.source == "flaky" and r.ok for r in q.readings)
    check("recovered source is re-admitted", healed,
          f"breaker={flaky.breaker.state.value}, tier={q.tier.value}")


async def s9_garbage_prices():
    print("\n9. GARBAGE INPUT — negative and zero prices")
    q = await Reconciler([LyingSource("neg", -50.0), LyingSource("zero", 0.0), HEALTHY("ok", 100.0)]).reconcile("TCS")
    check("non-positive prices are rejected", q.price == 100.0,
          f"resolved {q.price:.2f} from the only valid source")
    check("rejects counted against coverage", q.coverage < 1.0, f"coverage={q.coverage:.2f}")


async def s10_nan_history():
    print("\n10. CORRUPT HISTORY — NaN in the price series")
    q = await Reconciler([HEALTHY("a", 110.0), HEALTHY("b", 110.1)]).reconcile("TCS")
    closes = [100.0, 101.0, float("nan"), 102.0, float("inf"), 101.0, 103.0, 102.0]
    try:
        r = detect_significance(q, daily_closes=closes)
        check("detector survives corrupt history", True, f"reason={r.reason.value}, no crash")
    except Exception as e:
        check("detector survives corrupt history", False, f"CRASHED: {type(e).__name__}: {e}")


async def main():
    print("CHAOS TESTING — fault injection against the real pipeline")
    print("Invariant under test: never present a fabricated price as fact.")
    print("=" * 70)
    for s in (s1_total_blackout, s2_single_survivor, s3_adapter_raises,
              s4_hanging_source, s5_outlier_liar, s6_stale_data,
              s7_breaker_opens, s8_breaker_recovers, s9_garbage_prices,
              s10_nan_history):
        try:
            await s()
        except Exception as e:
            check(s.__name__, False, f"SCENARIO CRASHED: {type(e).__name__}: {e}")

    print("\n" + "=" * 70)
    passed = sum(1 for r, _, _ in results if r == PASS)
    print(f"{passed}/{len(results)} invariants held")
    for r, name, detail in results:
        if r == FAIL:
            print(f"  FAILED: {name} — {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
