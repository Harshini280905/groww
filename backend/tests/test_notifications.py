"""Unit tests for notifications.py — priority classification and the
ConnectionManager's fan-out/dead-connection-cleanup behavior."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.notifications import ConnectionManager, Priority, priority_for
from app.significance import SignificantEvent


def _event(z_score: float) -> SignificantEvent:
    return SignificantEvent(
        symbol="TCS", ts=datetime.now(timezone.utc),
        z_score=z_score, volume_ratio=1.0, direction="up",
        price_before=100.0, price_after=105.0,
        confidence=0.9, note="test",
    )


class PriorityClassification(unittest.TestCase):
    def test_huge_move_is_p0_regardless_of_intent(self):
        self.assertEqual(priority_for(_event(4.5), intent_tag=None), Priority.P0_IMMEDIATE)

    def test_moderate_move_own_it_tag_is_p0(self):
        # z=3.2 alone would be P1, but own_it tag lowers the P0 bar to 3.0
        self.assertEqual(priority_for(_event(3.2), intent_tag="own_it"), Priority.P0_IMMEDIATE)

    def test_moderate_move_no_tag_is_p1(self):
        self.assertEqual(priority_for(_event(2.5), intent_tag=None), Priority.P1_BATCHED)

    def test_moderate_move_own_it_tag_below_p0_bar_is_p1(self):
        self.assertEqual(priority_for(_event(2.8), intent_tag="own_it"), Priority.P1_BATCHED)

    def test_small_move_is_p2(self):
        self.assertEqual(priority_for(_event(1.5), intent_tag=None), Priority.P2_DIGEST)

    def test_negative_z_score_uses_absolute_value(self):
        self.assertEqual(priority_for(_event(-4.5), intent_tag=None), Priority.P0_IMMEDIATE)


class _FakeWebSocket:
    def __init__(self, fail=False):
        self.sent: list[str] = []
        self.fail = fail
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_text(self, msg):
        if self.fail:
            raise RuntimeError("connection closed")
        self.sent.append(msg)


class ConnectionManagerBehavior(unittest.IsolatedAsyncioTestCase):
    async def test_connect_then_push_delivers(self):
        mgr = ConnectionManager()
        ws = _FakeWebSocket()
        await mgr.connect(user_id=1, ws=ws)
        self.assertTrue(ws.accepted)
        sent = await mgr.push(1, {"hello": "world"})
        self.assertEqual(sent, 1)
        self.assertEqual(len(ws.sent), 1)

    async def test_push_to_user_with_no_connections_returns_zero(self):
        mgr = ConnectionManager()
        sent = await mgr.push(999, {"hello": "world"})
        self.assertEqual(sent, 0)

    async def test_dead_connection_is_cleaned_up_on_push(self):
        mgr = ConnectionManager()
        dead_ws = _FakeWebSocket(fail=True)
        await mgr.connect(user_id=5, ws=dead_ws)
        self.assertEqual(mgr.connection_count(5), 1)
        sent = await mgr.push(5, {"x": 1})
        self.assertEqual(sent, 0)
        self.assertEqual(mgr.connection_count(5), 0)   # pruned

    async def test_multiple_connections_same_user_both_receive(self):
        mgr = ConnectionManager()
        ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
        await mgr.connect(user_id=2, ws=ws1)
        await mgr.connect(user_id=2, ws=ws2)
        sent = await mgr.push(2, {"x": 1})
        self.assertEqual(sent, 2)

    async def test_disconnect_removes_connection(self):
        mgr = ConnectionManager()
        ws = _FakeWebSocket()
        await mgr.connect(user_id=3, ws=ws)
        mgr.disconnect(3, ws)
        self.assertEqual(mgr.connection_count(3), 0)


if __name__ == "__main__":
    unittest.main()
