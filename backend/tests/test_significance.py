"""Unit tests for significance.py — the z-score detector."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.market_data import ConfidenceTier, ReconciledQuote
from app.significance import (
    BEST_AVAILABLE_THRESHOLDS,
    MIN_HISTORY_BARS,
    SignificanceReason,
    VERIFIED_THRESHOLDS,
    compute_volume_ratio,
    compute_z_score,
    detect_significance,
    trailing_volatility,
)


def _quote(price: float, tier: ConfidenceTier = ConfidenceTier.VERIFIED,
           volume: float | None = None, confidence: float = 0.9) -> ReconciledQuote:
    return ReconciledQuote(
        symbol="TCS",
        price=price,
        volume=volume,
        resolved_at=datetime.now(timezone.utc),
        confidence=confidence,
        tier=tier,
        coverage=1.0, agreement=1.0, freshness=1.0,
        readings=(),
    )


# A synthetic history of a "typical" stock — ±1% daily moves.
# Trailing σ works out to ~0.8%, mean ~0. That makes:
#   +1% today  → z ≈ +1.2   (below solo, near combined)
#   +2% today  → z ≈ +2.5   (above solo — fires on VERIFIED)
#   +4% today  → z ≈ +5.0   (fires even on BEST_AVAILABLE)
_TYPICAL_CLOSES = [
    100.0, 100.8, 100.2, 101.0, 100.4, 101.1, 100.6,
    101.3, 100.5, 101.4, 100.7, 101.5, 100.8, 101.6,
    100.9, 101.7, 101.0, 101.8, 101.1, 102.0,
    # Last element = "previous close" by default
]
_TYPICAL_VOLUMES = [1_000_000.0] * len(_TYPICAL_CLOSES)


class MathHelpers(unittest.TestCase):
    def test_trailing_volatility_flat_history_is_zero(self):
        mean, sd = trailing_volatility([100.0] * 21)
        self.assertEqual(mean, 0.0)
        self.assertEqual(sd, 0.0)

    def test_trailing_volatility_reasonable_range(self):
        mean, sd = trailing_volatility(_TYPICAL_CLOSES)
        self.assertGreater(sd, 0.003)
        self.assertLess(sd, 0.020)

    def test_z_score_none_when_sd_zero(self):
        self.assertIsNone(compute_z_score(100.0, 100.0, mean_return=0.0, stddev_return=0.0))

    def test_z_score_matches_expected_sign(self):
        z = compute_z_score(102.0, 100.0, mean_return=0.0, stddev_return=0.01)
        self.assertAlmostEqual(z, 2.0)

    def test_volume_ratio_missing_data_returns_one(self):
        self.assertEqual(compute_volume_ratio(None, 1.0), 1.0)
        self.assertEqual(compute_volume_ratio(1.0, None), 1.0)
        self.assertEqual(compute_volume_ratio(1.0, 0.0), 1.0)


class TierGating(unittest.TestCase):
    """The safety belt: UNCONFIRMED never fires, BEST_AVAILABLE requires more."""

    def test_unconfirmed_never_fires_even_on_huge_move(self):
        # 10% move — massive — but the quote is unconfirmed
        q = _quote(price=110.0, tier=ConfidenceTier.UNCONFIRMED, confidence=0.2)
        r = detect_significance(q, _TYPICAL_CLOSES, _TYPICAL_VOLUMES)
        self.assertFalse(r.is_significant)
        self.assertEqual(r.reason, SignificanceReason.UNCONFIRMED_QUOTE)

    def test_verified_fires_on_moderate_move(self):
        # +2% move → z ≈ 2.5 → above VERIFIED z_solo
        q = _quote(price=104.04, tier=ConfidenceTier.VERIFIED)
        r = detect_significance(q, _TYPICAL_CLOSES, _TYPICAL_VOLUMES)
        self.assertTrue(r.is_significant)
        self.assertEqual(r.reason, SignificanceReason.SIGNIFICANT_Z)
        self.assertGreater(r.z_score, VERIFIED_THRESHOLDS.z_solo)

    def test_best_available_does_not_fire_on_same_move_verified_would(self):
        # +2% move — enough for VERIFIED but not BEST_AVAILABLE (threshold 3.0)
        q = _quote(price=104.04, tier=ConfidenceTier.BEST_AVAILABLE)
        r = detect_significance(q, _TYPICAL_CLOSES, _TYPICAL_VOLUMES)
        self.assertFalse(r.is_significant)
        self.assertEqual(r.reason, SignificanceReason.BELOW_THRESHOLD)

    def test_best_available_fires_on_larger_move(self):
        # +5% move → z ≈ 6 → above BEST_AVAILABLE z_solo (3.0)
        q = _quote(price=107.1, tier=ConfidenceTier.BEST_AVAILABLE)
        r = detect_significance(q, _TYPICAL_CLOSES, _TYPICAL_VOLUMES)
        self.assertTrue(r.is_significant)


class CircuitAndColdStart(unittest.TestCase):
    def test_in_exchange_circuit_suspends_detection(self):
        q = _quote(price=110.0, tier=ConfidenceTier.VERIFIED)   # 8% move
        r = detect_significance(q, _TYPICAL_CLOSES, is_in_circuit=True)
        self.assertFalse(r.is_significant)
        self.assertEqual(r.reason, SignificanceReason.IN_EXCHANGE_CIRCUIT)

    def test_insufficient_history_bails_out(self):
        q = _quote(price=110.0)
        r = detect_significance(q, daily_closes=[100.0, 101.0])   # only 2 bars
        self.assertFalse(r.is_significant)
        self.assertEqual(r.reason, SignificanceReason.INSUFFICIENT_HISTORY)
        self.assertLess(len([100.0, 101.0]), MIN_HISTORY_BARS)


class ZeroVolatilityEdgeCase(unittest.TestCase):
    def test_flat_history_small_move_does_not_fire(self):
        q = _quote(price=100.5)   # 0.5% move on flat history
        r = detect_significance(q, [100.0] * 21)
        self.assertFalse(r.is_significant)
        self.assertEqual(r.reason, SignificanceReason.ZERO_VOLATILITY_NO_MOVE)

    def test_flat_history_large_move_fires_via_fallback(self):
        q = _quote(price=106.0)   # 6% move on flat history
        r = detect_significance(q, [100.0] * 21)
        self.assertTrue(r.is_significant)
        self.assertEqual(r.reason, SignificanceReason.SIGNIFICANT_FLAT_FALLBACK)
        self.assertIsNotNone(r.event)


class VolumeCombinedTrigger(unittest.TestCase):
    def test_moderate_z_plus_high_volume_fires(self):
        # +1.3% move → z ≈ 1.6 → between z_combined (1.2) and z_solo (2.0)
        # Boost volume to 2.5x → triggers combined
        q = _quote(price=103.33, volume=2_500_000.0, tier=ConfidenceTier.VERIFIED)
        r = detect_significance(q, _TYPICAL_CLOSES, _TYPICAL_VOLUMES)
        self.assertTrue(r.is_significant)
        self.assertEqual(r.reason, SignificanceReason.SIGNIFICANT_Z_VOLUME)

    def test_moderate_z_normal_volume_does_not_fire(self):
        q = _quote(price=103.33, volume=1_000_000.0)   # normal volume
        r = detect_significance(q, _TYPICAL_CLOSES, _TYPICAL_VOLUMES)
        self.assertFalse(r.is_significant)


class EventPayload(unittest.TestCase):
    def test_direction_up_and_down(self):
        q_up = _quote(price=105.0)
        r_up = detect_significance(q_up, _TYPICAL_CLOSES, _TYPICAL_VOLUMES)
        self.assertTrue(r_up.is_significant)
        self.assertEqual(r_up.event.direction, "up")

        q_down = _quote(price=99.0)
        r_down = detect_significance(q_down, _TYPICAL_CLOSES, _TYPICAL_VOLUMES)
        self.assertTrue(r_down.is_significant)
        self.assertEqual(r_down.event.direction, "down")

    def test_event_carries_confidence_from_quote(self):
        q = _quote(price=105.0, confidence=0.87)
        r = detect_significance(q, _TYPICAL_CLOSES, _TYPICAL_VOLUMES)
        self.assertTrue(r.is_significant)
        self.assertAlmostEqual(r.event.confidence, 0.87)


if __name__ == "__main__":
    unittest.main()
