"""Unit tests for the cycle detection algorithm."""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.algorithm import detect_cycles  # noqa: E402


def ah(left_current_a: float, dt_s: int) -> float:
    return abs(left_current_a) * dt_s / 3600.0


class OneCycleTests(unittest.TestCase):
    def test_basic_zero_plus_zero_minus_zero(self):
        # 0s:0, 10s:+2A, 20s:0 (charge interval 10s @ 2A = 20/3600 Ah)
        # 30s:-3A, 40s:0 (discharge interval 10s @ 3A = 30/3600 Ah)
        samples = [(0, 0.0), (10, 2.0), (20, 0.0), (30, -3.0), (40, 0.0)]
        cycles = detect_cycles(samples)
        self.assertEqual(len(cycles), 1)
        c = cycles[0]
        self.assertEqual(c.cycle_no, 1)
        self.assertAlmostEqual(c.charge_ah, ah(2, 10))
        self.assertAlmostEqual(c.discharge_ah, ah(3, 10))
        self.assertEqual((c.started_at, c.ended_at), (0, 40))

    def test_multi_interval_left_value_integration(self):
        # charge: +2A for 10s then +4A for 10s then zero
        samples = [
            (0, 0.0),
            (10, 2.0),
            (20, 4.0),
            (30, 0.0),
            (40, -1.0),
            (50, 0.0),
        ]
        cycles = detect_cycles(samples)
        self.assertEqual(len(cycles), 1)
        self.assertAlmostEqual(cycles[0].charge_ah, ah(2, 10) + ah(4, 10))
        self.assertAlmostEqual(cycles[0].discharge_ah, ah(1, 10))

    def test_consecutive_zeros_are_harmless(self):
        samples = [
            (0, 0.0),
            (5, 0.0),
            (10, 1.0),
            (20, 0.0),
            (25, 0.0),
            (30, -1.0),
            (40, 0.0),
        ]
        cycles = detect_cycles(samples)
        self.assertEqual(len(cycles), 1)

    def test_unclosed_tail_is_not_counted(self):
        # full cycle then an unclosed positive segment
        samples = [
            (0, 0.0),
            (10, 1.0),
            (20, 0.0),
            (30, -1.0),
            (40, 0.0),
            (50, 1.0),
            (60, 1.0),
        ]
        cycles = detect_cycles(samples)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].ended_at, 40)

    def test_negative_first_does_not_start_cycle(self):
        samples = [(0, -1.0), (10, 0.0), (20, 1.0), (30, 0.0), (40, -1.0), (50, 0.0)]
        cycles = detect_cycles(samples)
        self.assertEqual(len(cycles), 1)
        self.assertAlmostEqual(cycles[0].charge_ah, ah(1, 10))


class GapTests(unittest.TestCase):
    def test_gap_over_60s_truncates_sequence(self):
        # positive charge, then gap > 60s, then zero/negative/zero:
        # the pre-gap positive cannot be part of a cycle
        samples = [
            (0, 0.0),
            (10, 2.0),
            (71, 0.0),   # gap of 61 s -> truncate
            (80, -3.0),
            (90, 0.0),
        ]
        cycles = detect_cycles(samples)
        self.assertEqual(cycles, [])

    def test_gap_exactly_60s_does_not_truncate(self):
        samples = [
            (0, 0.0),
            (60, 2.0),   # dt = 60, still allowed
            (120, 0.0),
            (130, -1.0),
            (140, 0.0),
        ]
        cycles = detect_cycles(samples)
        self.assertEqual(len(cycles), 1)

    def test_gap_interval_excluded_from_integral(self):
        # the long interval itself must not contribute Ah
        samples = [
            (0, 0.0),
            (10, 2.0),
            (20, 0.0),
            (30, -2.0),
            (100, 0.0),  # 70 s gap during discharge: interval dropped
        ]
        cycles = detect_cycles(samples)
        # sequence broken, no closing zero within 60 s -> no cycle
        self.assertEqual(cycles, [])

    def test_gap_then_new_cycle(self):
        samples = [
            (0, 0.0),
            (10, 2.0),
            (20, 0.0),
            (30, -2.0),
            (40, 0.0),   # complete cycle #1
            (200, 0.0),  # gap, new start
            (210, 1.0),
            (220, 0.0),
            (230, -1.0),
            (240, 0.0),  # complete cycle #2
        ]
        cycles = detect_cycles(samples)
        self.assertEqual([c.cycle_no for c in cycles], [1, 2])
        self.assertEqual(cycles[1].started_at, 200)


class OrderingTests(unittest.TestCase):
    def test_sign_flip_without_zero_is_invalid(self):
        # + directly followed by - (no zero boundary): no cycle
        samples = [
            (0, 0.0),
            (10, 2.0),
            (20, -2.0),
            (30, 0.0),
            (40, 0.0),
        ]
        cycles = detect_cycles(samples)
        self.assertEqual(cycles, [])

    def test_two_back_to_back_cycles_share_boundary_zero(self):
        samples = [
            (0, 0.0),
            (10, 1.0),
            (20, 0.0),
            (30, -1.0),
            (40, 0.0),
            (50, 1.0),
            (60, 0.0),
            (70, -1.0),
            (80, 0.0),
        ]
        cycles = detect_cycles(samples)
        self.assertEqual(len(cycles), 2)
        self.assertEqual([c.cycle_no for c in cycles], [1, 2])

    def test_charge_without_discharge_not_counted(self):
        samples = [(0, 0.0), (10, 1.0), (20, 0.0), (30, 1.0), (40, 0.0)]
        self.assertEqual(detect_cycles(samples), [])

    def test_empty_input(self):
        self.assertEqual(detect_cycles([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
