"""Unit tests for scheduler.py's pure market-hours logic.

Does NOT test the actual APScheduler wiring (start_scheduler/stop_scheduler)
since that requires a running event loop and touches real process state —
market_is_open is the part worth unit-testing because it's pure and it's
the exact function that decides whether the scheduler does any work at all.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduler import IST, market_is_open


def _ist(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=IST)


class MarketHours(unittest.TestCase):
    def test_mid_session_weekday_is_open(self):
        # Friday, 2026-09-04 (today, per system context), noon IST
        self.assertTrue(market_is_open(_ist(2026, 9, 4, 12, 0)))

    def test_before_open_is_closed(self):
        self.assertFalse(market_is_open(_ist(2026, 9, 4, 9, 0)))

    def test_after_close_is_closed(self):
        self.assertFalse(market_is_open(_ist(2026, 9, 4, 15, 45)))

    def test_exactly_at_open_boundary_is_open(self):
        self.assertTrue(market_is_open(_ist(2026, 9, 4, 9, 15)))

    def test_exactly_at_close_boundary_is_open(self):
        self.assertTrue(market_is_open(_ist(2026, 9, 4, 15, 30)))

    def test_saturday_is_closed(self):
        # 2026-09-05 is a Saturday
        self.assertFalse(market_is_open(_ist(2026, 9, 5, 12, 0)))

    def test_sunday_is_closed(self):
        # 2026-09-06 is a Sunday
        self.assertFalse(market_is_open(_ist(2026, 9, 6, 12, 0)))

    def test_naive_now_defaults_correctly(self):
        # Just verify it doesn't raise when called with no argument.
        result = market_is_open()
        self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main()
