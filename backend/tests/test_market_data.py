"""Unit tests for market_data.py — the load-bearing reconciliation layer.

These tests do NOT hit any real market source. Adapters are stubbed to return
controlled readings so we can assert on confidence math, tier boundaries,
circuit-breaker state machine, queue discipline, and rate cap behavior.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from datetime import datetime, timedelta, timezone

from app.market_data import (
    CircuitBreaker,
    CircuitLimits,
    CircuitState,
    ConfidenceTier,
    RateCap,
    Reconciler,
    SourceReading,
    SymbolQueues,
    compute_confidence,
    is_in_exchange_circuit,
    poll_symbol,
    tier_for,
)


def _reading(source: str, price: float, seconds_old: float = 0.0, error: str | None = None) -> SourceReading:
    return SourceReading(
        source=source,
        symbol="TCS",
        price=price,
        volume=1000.0,
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_old),
        latency_ms=42.0,
        error=error,
    )


class TierBoundaries(unittest.TestCase):
    def test_verified_at_or_above_080(self):
        self.assertEqual(tier_for(0.80), ConfidenceTier.VERIFIED)
        self.assertEqual(tier_for(0.99), ConfidenceTier.VERIFIED)

    def test_best_between_050_and_080(self):
        self.assertEqual(tier_for(0.50), ConfidenceTier.BEST_AVAILABLE)
        self.assertEqual(tier_for(0.79), ConfidenceTier.BEST_AVAILABLE)

    def test_unconfirmed_below_050(self):
        self.assertEqual(tier_for(0.49), ConfidenceTier.UNCONFIRMED)
        self.assertEqual(tier_for(0.0), ConfidenceTier.UNCONFIRMED)


class ConfidenceMath(unittest.TestCase):
    def test_two_agreeing_fresh_sources_score_high(self):
        readings = [_reading("yahoo", 1417.20, 5.0), _reading("nse", 1418.55, 5.0)]
        confidence, cov, agr, fr = compute_confidence(readings, configured_sources=2)
        # spread ~0.095%, well under 2% cap → agreement ~0.95
        self.assertAlmostEqual(cov, 1.0)
        self.assertGreater(agr, 0.9)
        self.assertGreater(fr, 0.9)
        self.assertGreaterEqual(confidence, VERIFIED_MIN := 0.80)

    def test_missing_source_drops_coverage_but_not_agreement(self):
        readings = [_reading("yahoo", 1417.20, 2.0), _reading("nse", 0.0, 0.0, error="timeout")]
        confidence, cov, agr, fr = compute_confidence(readings, configured_sources=2)
        self.assertAlmostEqual(cov, 0.5)
        # One source can't disagree with itself
        self.assertEqual(agr, 1.0)
        # Confidence math alone yields ~0.82 here — high, but the tier rule (below)
        # explicitly demotes single-source quotes out of VERIFIED even so.
        self.assertGreaterEqual(confidence, 0.50)


class TierInvariants(unittest.TestCase):
    """Guardrails for the "VERIFIED means we cross-checked" promise."""

    def test_single_source_cannot_be_verified_even_with_high_confidence(self):
        # A single healthy source scoring high (0.95) is still BEST_AVAILABLE:
        # we haven't actually verified anything against a second source.
        self.assertEqual(
            tier_for(0.95, n_ok_sources=1),
            ConfidenceTier.BEST_AVAILABLE,
        )

    def test_zero_sources_low_confidence_is_unconfirmed(self):
        self.assertEqual(
            tier_for(0.10, n_ok_sources=0),
            ConfidenceTier.UNCONFIRMED,
        )

    def test_two_sources_high_confidence_is_verified(self):
        self.assertEqual(
            tier_for(0.90, n_ok_sources=2),
            ConfidenceTier.VERIFIED,
        )

    def test_wide_disagreement_saturates_agreement_to_zero(self):
        # 3% spread — well past the 2% cap
        readings = [_reading("yahoo", 1400.0, 1.0), _reading("nse", 1444.0, 1.0)]
        confidence, cov, agr, fr = compute_confidence(readings, configured_sources=2)
        self.assertEqual(agr, 0.0)
        self.assertLess(confidence, 0.80)

    def test_stale_data_kills_freshness(self):
        # 180s old — 2x the staleness cap
        readings = [_reading("yahoo", 1417.20, 180.0), _reading("nse", 1418.55, 180.0)]
        confidence, cov, agr, fr = compute_confidence(readings, configured_sources=2)
        self.assertEqual(fr, 0.0)

    def test_all_sources_failed_gives_zero_confidence(self):
        readings = [
            _reading("yahoo", 0.0, 0.0, error="conn_refused"),
            _reading("nse", 0.0, 0.0, error="timeout"),
        ]
        confidence, cov, agr, fr = compute_confidence(readings, configured_sources=2)
        self.assertEqual(confidence, 0.0)
        self.assertEqual(tier_for(confidence), ConfidenceTier.UNCONFIRMED)


class CircuitBreakerStateMachine(unittest.TestCase):
    def test_starts_closed_and_allows(self):
        cb = CircuitBreaker("test")
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.allow())

    def test_opens_after_threshold_consecutive_failures(self):
        cb = CircuitBreaker("test", failure_threshold=3, base_cooldown_s=30)
        for _ in range(3):
            self.assertTrue(cb.allow())
            cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.allow())

    def test_single_success_resets_failure_count(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        # Two more failures should NOT open — counter was reset
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_cooldown_grows_exponentially(self):
        cb = CircuitBreaker("test", failure_threshold=1, base_cooldown_s=30, max_cooldown_s=300)
        cb.record_failure()
        first = cb._current_cooldown_s
        # Simulate cooldown expiry → half-open → probe fails → open again
        cb._opened_at = time.monotonic() - 999
        self.assertTrue(cb.allow())               # half-open probe
        cb.record_failure()
        second = cb._current_cooldown_s
        self.assertGreater(second, first)

    def test_cooldown_caps_at_max(self):
        cb = CircuitBreaker("test", failure_threshold=1, base_cooldown_s=100, max_cooldown_s=250)
        cb.record_failure()
        for _ in range(10):
            cb._opened_at = time.monotonic() - 9999
            cb.allow()
            cb.record_failure()
        self.assertLessEqual(cb._current_cooldown_s, 250)


class SymbolQueueDiscipline(unittest.TestCase):
    def test_drop_oldest_on_overflow(self):
        async def run():
            q = SymbolQueues(maxsize=3)
            fake_quote = lambda i: _fake_reconciled(price=100.0 + i)
            for i in range(3):
                displaced = q.enqueue("TCS", fake_quote(i))
                self.assertFalse(displaced)
            # 4th enqueue should displace oldest (price=100.0)
            displaced = q.enqueue("TCS", fake_quote(99))
            self.assertTrue(displaced)
            # Confirm we now hold prices [101, 102, 199]
            drained = [(await q.dequeue("TCS")).price for _ in range(3)]
            self.assertEqual(drained, [101.0, 102.0, 199.0])
        asyncio.run(run())


class RateCapBehavior(unittest.TestCase):
    def test_second_call_within_interval_denied(self):
        rc = RateCap(interval_s=1.0)
        self.assertTrue(rc.allow("TCS"))
        self.assertFalse(rc.allow("TCS"))       # too soon
        self.assertTrue(rc.allow("INFY"))       # different symbol, unrelated

    def test_call_after_interval_allowed(self):
        rc = RateCap(interval_s=0.05)
        self.assertTrue(rc.allow("TCS"))
        time.sleep(0.06)
        self.assertTrue(rc.allow("TCS"))


class ExchangeCircuitAwareness(unittest.TestCase):
    def setUp(self):
        # Observed live from Groww for RELIANCE on 2026-09-04
        self.reliance = CircuitLimits(
            symbol="RELIANCE", upper=1432.70, lower=1172.30, as_of_date="2026-09-04"
        )

    def test_price_mid_range_not_circuited(self):
        self.assertFalse(is_in_exchange_circuit(1327.60, self.reliance))

    def test_price_at_upper_bound_circuited(self):
        self.assertTrue(is_in_exchange_circuit(1432.70, self.reliance))

    def test_price_within_epsilon_of_upper_circuited(self):
        near_upper = 1432.70 * 0.9995         # within 0.1% band
        self.assertTrue(is_in_exchange_circuit(near_upper, self.reliance))

    def test_price_at_lower_bound_circuited(self):
        self.assertTrue(is_in_exchange_circuit(1172.30, self.reliance))

    def test_zero_price_never_circuited(self):
        self.assertFalse(is_in_exchange_circuit(0.0, self.reliance))


class ReconcilerFanOut(unittest.IsolatedAsyncioTestCase):
    async def test_uses_median_across_sources(self):
        """A broken source returning a wildly-wrong price must NOT drag the
        resolved price — median is robust to a single outlier."""
        r = Reconciler([_StubSource("yahoo", 1000.0), _StubSource("nse", 1002.0), _StubSource("bad", 500.0)])
        quote = await r.reconcile("TCS")
        # median([1000, 1002, 500]) = 1000
        self.assertEqual(quote.price, 1000.0)

    async def test_open_breaker_short_circuits_call(self):
        stub = _StubSource("yahoo", 1000.0)
        # Force the breaker open
        for _ in range(stub.breaker.failure_threshold):
            stub.breaker.record_failure()
        r = Reconciler([stub, _StubSource("nse", 1002.0)])
        quote = await r.reconcile("TCS")
        # Only nse contributed
        contributing = [rd for rd in quote.readings if rd.ok]
        self.assertEqual(len(contributing), 1)
        self.assertEqual(contributing[0].source, "nse")
        # yahoo reading marked circuit_open
        yahoo_rd = next(rd for rd in quote.readings if rd.source == "yahoo")
        self.assertEqual(yahoo_rd.error, "circuit_open")

    async def test_adapter_exception_does_not_poison_batch(self):
        r = Reconciler([_ExplodingSource("boom"), _StubSource("nse", 1002.0)])
        quote = await r.reconcile("TCS")
        self.assertEqual(quote.price, 1002.0)      # nse's single reading survives
        # Only one source contributed → tier invariant demotes out of VERIFIED
        self.assertEqual(quote.tier, ConfidenceTier.BEST_AVAILABLE)

    async def test_all_sources_fail_yields_unconfirmed(self):
        r = Reconciler([_ExplodingSource("a"), _ExplodingSource("b")])
        quote = await r.reconcile("TCS")
        self.assertEqual(quote.price, 0.0)
        self.assertEqual(quote.tier, ConfidenceTier.UNCONFIRMED)


class PollSymbolIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_rate_cap_gates_second_call(self):
        r = Reconciler([_StubSource("yahoo", 100.0)])
        rc = RateCap(interval_s=1.0)
        q = SymbolQueues(maxsize=5)
        first = await poll_symbol("TCS", r, rc, q)
        self.assertIsNotNone(first)
        second = await poll_symbol("TCS", r, rc, q)
        self.assertIsNone(second)


# ─── Test fixtures ──────────────────────────────────────────────────────────

def _fake_reconciled(price: float):
    """Minimal ReconciledQuote for queue tests — doesn't touch reconciler."""
    from app.market_data import ReconciledQuote
    return ReconciledQuote(
        symbol="TCS",
        price=price,
        volume=None,
        resolved_at=datetime.now(timezone.utc),
        confidence=1.0,
        tier=ConfidenceTier.VERIFIED,
        coverage=1.0,
        agreement=1.0,
        freshness=1.0,
        readings=(),
    )


class _StubSource:
    """Adapter that returns a controlled price and never errors."""
    def __init__(self, name: str, price: float):
        self.name = name
        self._price = price
        self.breaker = CircuitBreaker(name)

    async def fetch(self, symbol: str) -> SourceReading:
        return SourceReading(
            source=self.name,
            symbol=symbol,
            price=self._price,
            volume=1000.0,
            fetched_at=datetime.now(timezone.utc),
            latency_ms=1.0,
        )


class _ExplodingSource:
    """Adapter that raises — exercises the reconciler's belt-and-suspenders."""
    def __init__(self, name: str):
        self.name = name
        self.breaker = CircuitBreaker(name)

    async def fetch(self, symbol: str) -> SourceReading:
        raise RuntimeError("boom")


if __name__ == "__main__":
    unittest.main()
