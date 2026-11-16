"""Unit tests for measured-segment extraction and step/segment matching."""

import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.algorithm import extract_measured_segments  # noqa: E402
from app.diagnostics import Step, diagnose  # noqa: E402


def seg(no, polarity, start, end, capacity=0.0):
    return {
        "segment_no": no,
        "polarity": polarity,
        "start": start,
        "end": end,
        "capacity": capacity,
    }


class SegmentExtractionTests(unittest.TestCase):
    def test_basic_zero_closed_runs(self):
        # 0->10 charge, 10->20 flat 2A? no: sample at 20 is the closing zero
        samples = [(0, 0.0), (10, 2.0), (20, 0.0), (30, -3.0), (40, 0.0)]
        segments = extract_measured_segments(samples)
        self.assertEqual(len(segments), 2)
        first, second = segments
        self.assertEqual((first.segment_no, first.polarity), (1, 1))
        self.assertEqual((first.started_at, first.ended_at), (10, 20))
        self.assertAlmostEqual(first.capacity_ah, 2.0 * 10 / 3600)
        self.assertEqual((second.segment_no, second.polarity), (2, -1))
        self.assertEqual((second.started_at, second.ended_at), (30, 40))
        self.assertAlmostEqual(second.capacity_ah, 3.0 * 10 / 3600)

    def test_multi_interval_left_value_capacity(self):
        samples = [(0, 0.0), (10, 2.0), (20, 4.0), (30, 0.0)]
        [segment] = extract_measured_segments(samples)
        self.assertAlmostEqual(
            segment.capacity_ah, (2.0 * 10 + 4.0 * 10) / 3600
        )

    def test_run_starting_on_non_zero_is_still_extracted(self):
        # the run is zero-closed and gap-free; its start is the first sample
        # with that polarity (the data start is a valid boundary)
        samples = [(0, 2.0), (10, 2.0), (20, 0.0)]
        [segment] = extract_measured_segments(samples)
        self.assertEqual((segment.started_at, segment.ended_at), (0, 20))
        self.assertAlmostEqual(segment.capacity_ah, 2.0 * 20 / 3600)

    def test_unclosed_tail_is_not_extracted(self):
        samples = [(0, 0.0), (10, 2.0), (20, 2.0)]
        self.assertEqual(extract_measured_segments(samples), [])

    def test_gap_inside_run_splits_and_drops_unclosed_part(self):
        # the 70 s interval contributes no charge and cannot be bridged; the
        # post-gap run is zero-closed and gap-free, so it is extracted
        samples = [(0, 0.0), (10, 2.0), (80, 2.0), (90, 0.0)]
        [segment] = extract_measured_segments(samples)
        self.assertEqual((segment.started_at, segment.ended_at), (80, 90))
        self.assertAlmostEqual(segment.capacity_ah, 2.0 * 10 / 3600)

    def test_gap_after_open_run_discards_it(self):
        samples = [(0, 0.0), (10, 2.0), (80, 0.0)]
        self.assertEqual(extract_measured_segments(samples), [])

    def test_gap_exactly_60s_keeps_run(self):
        samples = [(0, 0.0), (60, 2.0), (120, 0.0)]
        [segment] = extract_measured_segments(samples)
        self.assertEqual((segment.started_at, segment.ended_at), (60, 120))
        self.assertAlmostEqual(segment.capacity_ah, 2.0 * 60 / 3600)

    def test_gap_between_runs_does_not_affect_next_run(self):
        samples = [
            (0, 0.0),
            (10, 1.0),
            (20, 0.0),
            (200, 0.0),  # gap after the closing zero
            (210, -1.0),
            (220, 0.0),
        ]
        segments = extract_measured_segments(samples)
        self.assertEqual([s.polarity for s in segments], [1, -1])
        self.assertEqual(segments[1].started_at, 210)

    def test_sign_flip_without_zero_ends_previous_run_unclosed(self):
        # +2 A is never zero-closed (a direct -2 A follows); the -2 A run is
        # zero-closed, so only it is extracted
        samples = [(0, 0.0), (10, 2.0), (20, -2.0), (30, 0.0)]
        [segment] = extract_measured_segments(samples)
        self.assertEqual(segment.polarity, -1)
        self.assertEqual((segment.started_at, segment.ended_at), (20, 30))
        self.assertAlmostEqual(segment.capacity_ah, 2.0 * 10 / 3600)

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
        segments = extract_measured_segments(samples)
        self.assertEqual([s.polarity for s in segments], [1, -1])


class MatchingTests(unittest.TestCase):
    def test_exact_pair_and_capacity(self):
        steps = [Step(1, 10, 20, 1), Step(2, 30, 40, -1)]
        samples = [(0, 0.0), (10, 2.0), (20, 0.0), (30, -3.0), (40, 0.0)]
        result = diagnose(steps, extract_measured_segments(samples))
        self.assertEqual(result["max_pairs"], 2)
        self.assertEqual(result["min_total_deviation"], 0)
        self.assertFalse(result["ambiguous"])
        self.assertEqual(len(result["matched"]), 2)
        self.assertEqual(
            [p["capacity_ah"] for p in result["matched"]],
            [2.0 * 10 / 3600, 3.0 * 10 / 3600],
        )
        self.assertEqual(result["unmatched_steps"], [])
        self.assertEqual(result["unmatched_segments"], [])

    def test_tolerance_is_inclusive_at_15s(self):
        steps = [Step(1, 15, 35, 1)]
        samples = [(0, 0.0), (0, 0.0)]
        segments = extract_measured_segments([(0, 0.0), (0, 2.0), (20, 0.0)])
        # measured 0..20, step 15..35 -> deviations 15 and 15
        result = diagnose(steps, segments)
        self.assertEqual(result["max_pairs"], 1)

    def test_tolerance_rejects_16s(self):
        steps = [Step(1, 16, 36, 1)]
        segments = extract_measured_segments([(0, 0.0), (0, 2.0), (20, 0.0)])
        result = diagnose(steps, segments)
        self.assertEqual(result["max_pairs"], 0)
        self.assertEqual(
            [s["step_no"] for s in result["unmatched_steps"]], [1]
        )
        self.assertEqual(
            [s["polarity"] for s in result["unmatched_segments"]], ["positive"]
        )

    def test_polarity_mismatch_is_unmatched(self):
        steps = [Step(1, 10, 20, -1)]
        segments = extract_measured_segments(
            [(0, 0.0), (10, 2.0), (20, 0.0)]
        )
        result = diagnose(steps, segments)
        self.assertEqual(result["max_pairs"], 0)
        self.assertEqual(result["unmatched_steps"][0]["step_no"], 1)
        self.assertEqual(result["unmatched_segments"][0]["segment_no"], 1)

    def test_missed_step_is_reported_and_alignment_resyncs(self):
        # planned +,+,+ but the middle measured segment is missing
        steps = [Step(1, 10, 20, 1), Step(2, 30, 40, 1), Step(3, 50, 60, 1)]
        samples = [
            (0, 0.0),
            (10, 1.0),
            (20, 0.0),   # segment 1 matches step 1
            (50, 1.0),
            (60, 0.0),   # segment 2 matches step 3
        ]
        result = diagnose(steps, extract_measured_segments(samples))
        self.assertEqual(result["max_pairs"], 2)
        self.assertEqual(
            [(p["step_no"], p["segment_no"]) for p in result["matched"]],
            [(1, 1), (3, 2)],
        )
        self.assertEqual(
            [s["step_no"] for s in result["unmatched_steps"]], [2]
        )

    def test_extra_measured_segment_is_reported(self):
        steps = [Step(1, 10, 20, 1)]
        samples = [
            (0, 0.0),
            (10, 1.0),
            (20, 0.0),
            (30, -1.0),
            (40, 0.0),
        ]
        result = diagnose(steps, extract_measured_segments(samples))
        self.assertEqual(result["max_pairs"], 1)
        self.assertEqual(
            [s["segment_no"] for s in result["unmatched_segments"]], [2]
        )

    def test_pair_count_beats_deviation(self):
        # pairing both steps costs 26; the single lowest-deviation pair costs 9
        steps = [Step(1, 100, 110, 1), Step(2, 112, 122, 1)]
        segments = [
            {"segment_no": 1, "polarity": 1, "started_at": 96, "ended_at": 103,
             "capacity_ah": 0.0},
            {"segment_no": 2, "polarity": 1, "started_at": 105, "ended_at": 114,
             "capacity_ah": 0.0},
        ]
        from app.algorithm import MeasuredSegment

        segs = [MeasuredSegment(**s) for s in segments]
        result = diagnose(steps, segs)
        self.assertEqual(result["max_pairs"], 2)
        self.assertEqual(result["min_total_deviation"], 26)
        self.assertEqual(
            [(p["step_no"], p["segment_no"]) for p in result["matched"]],
            [(1, 1), (2, 2)],
        )

    def test_tie_lists_all_candidates_and_no_arbitrary_choice(self):
        # one planned step fits two adjacent measured runs equally well
        steps = [Step(1, 100, 110, 1)]
        samples = [
            (90, 0.0),
            (100, 1.0),
            (104, 0.0),   # segment 1: 100..104, dev 0+6
            (105, 1.0),
            (109, 0.0),   # segment 2: 105..109, dev 5+1
        ]
        result = diagnose(steps, extract_measured_segments(samples))
        self.assertEqual(result["max_pairs"], 1)
        self.assertEqual(result["min_total_deviation"], 6)
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["candidate_count_exact"], 2)
        self.assertEqual(result["candidate_count"], 2)
        # no pair is common to both optima -> nothing is reported as definite
        self.assertEqual(result["matched"], [])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(
            sorted(
                p["segment_no"]
                for candidate in result["candidates"]
                for p in candidate["pairs"]
            ),
            [1, 2],
        )
        self.assertEqual(len(result["ambiguous_steps"]), 1)
        self.assertEqual(
            result["ambiguous_steps"][0]["candidate_segment_nos"], [1, 2]
        )
        self.assertEqual(
            [s["segment_no"] for s in result["ambiguous_segments"]], [1, 2]
        )
        self.assertEqual(result["unmatched_steps"], [])
        self.assertEqual(result["unmatched_segments"], [])

    def test_tie_between_two_steps_reports_candidates(self):
        # one measured run sits exactly between two short planned steps
        steps = [Step(1, 100, 105, 1), Step(2, 106, 111, 1)]
        segments = extract_measured_segments(
            [(95, 0.0), (95, 1.0), (116, 0.0)]
        )
        # measured 95..116: dev(step1)=5+11=16, dev(step2)=11+5=16
        result = diagnose(steps, segments)
        self.assertEqual(result["max_pairs"], 1)
        self.assertEqual(result["min_total_deviation"], 16)
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["matched"], [])
        self.assertEqual(
            sorted(s["step_no"] for s in result["ambiguous_steps"]), [1, 2]
        )
        self.assertEqual(
            result["ambiguous_segments"][0]["candidate_step_nos"], [1, 2]
        )
        self.assertEqual(
            [p["pairs"][0]["step_no"] for p in result["candidates"]], [1, 2]
        )

    def test_tied_optima_share_a_definite_pair(self):
        # step 1 has only one possible partner; step 2 is genuinely ambiguous
        steps = [Step(1, 100, 110, 1), Step(2, 200, 210, 1)]
        segments = extract_measured_segments(
            [
                (99, 0.0),
                (100, 1.0),
                (110, 0.0),   # segment 1 <-> step 1 exactly
            ]
        )
        from app.algorithm import MeasuredSegment

        segs = list(segments) + [
            MeasuredSegment(2, 1, 195, 205, 0.0),   # dev 5 + 5
            MeasuredSegment(3, 1, 205, 215, 0.0),   # dev 5 + 5 -> tie
        ]
        result = diagnose(steps, segs)
        self.assertEqual(result["max_pairs"], 2)
        self.assertTrue(result["ambiguous"])
        self.assertEqual(
            [(p["step_no"], p["segment_no"]) for p in result["matched"]],
            [(1, 1)],
        )
        self.assertEqual(
            sorted(s["step_no"] for s in result["ambiguous_steps"]), [2]
        )
        self.assertEqual(
            result["ambiguous_steps"][0]["candidate_segment_nos"], [2, 3]
        )
        # every candidate pairs both steps; only the segment assignment differs
        self.assertEqual(len(result["candidates"]), 2)
        seen_segment_orders = set()
        for candidate in result["candidates"]:
            self.assertEqual(
                sorted(p["step_no"] for p in candidate["pairs"]), [1, 2]
            )
            self.assertEqual(candidate["unmatched_step_nos"], [])
            by_step = {p["step_no"]: p["segment_no"] for p in candidate["pairs"]}
            self.assertEqual(by_step[1], 1)
            # one of segments 2/3 stays unmatched in each candidate
            self.assertEqual(
                candidate["unmatched_segment_nos"], [5 - by_step[2]]
            )
            seen_segment_orders.add(by_step[2])
        self.assertEqual(seen_segment_orders, {2, 3})

    def test_interleaved_order_never_crosses(self):
        # Both same-polarity steps could reach both runs, but a crossing pair
        # would violate time order; the DP must choose the non-crossing match.
        from app.algorithm import MeasuredSegment

        steps = [Step(1, 100, 105, 1), Step(2, 120, 125, 1)]
        segs = [
            MeasuredSegment(1, 1, 99, 104, 0.0),    # near step 1
            MeasuredSegment(2, 1, 121, 126, 0.0),   # near step 2
        ]
        result = diagnose(steps, segs)
        self.assertEqual(
            [(p["step_no"], p["segment_no"]) for p in result["matched"]],
            [(1, 1), (2, 2)],
        )
        self.assertEqual(result["min_total_deviation"], 4)

    def test_no_step_table_all_segments_unmatched(self):
        segments = extract_measured_segments(
            [(0, 0.0), (10, 1.0), (20, 0.0)]
        )
        result = diagnose([], segments)
        self.assertEqual(result["max_pairs"], 0)
        self.assertEqual(len(result["unmatched_segments"]), 1)
        self.assertFalse(result["ambiguous"])

    def test_no_segments_all_steps_unmatched(self):
        result = diagnose([Step(1, 0, 10, 1), Step(2, 20, 30, -1)], [])
        self.assertEqual(result["max_pairs"], 0)
        self.assertEqual(
            [s["step_no"] for s in result["unmatched_steps"]], [1, 2]
        )

    def test_empty_inputs(self):
        result = diagnose([], [])
        self.assertEqual(result["max_pairs"], 0)
        self.assertEqual(result["min_total_deviation"], 0)
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["candidates"], [{"pairs": [], "unmatched_step_nos": [], "unmatched_segment_nos": []}])


class BruteForceTests(unittest.TestCase):
    """Cross-check the DP against exhaustive enumeration on random inputs."""

    def _brute(self, steps, segments):
        n, m = len(steps), len(segments)

        def compatible(i, j):
            s, g = steps[i], segments[j]
            return (
                s.polarity == g.polarity
                and abs(s.start_ts - g.started_at) <= 15
                and abs(s.end_ts - g.ended_at) <= 15
            )

        matchings = []
        for r in range(0, min(n, m) + 1):
            for chosen_steps in itertools.combinations(range(n), r):
                for chosen_segs in itertools.combinations(range(m), r):
                    if all(
                        compatible(i, j)
                        for i, j in zip(chosen_steps, chosen_segs)
                    ):
                        matchings.append(tuple(zip(chosen_steps, chosen_segs)))

        def value(matching):
            return (
                len(matching),
                sum(
                    abs(steps[i].start_ts - segments[j].started_at)
                    + abs(steps[i].end_ts - segments[j].ended_at)
                    for i, j in matching
                ),
            )

        best_count = max((value(x)[0] for x in matchings), default=0)
        with_count = [x for x in matchings if value(x)[0] == best_count]
        best_cost = min((value(x)[1] for x in with_count), default=0)
        optimal = [x for x in with_count if value(x)[1] == best_cost]
        return best_count, best_cost, optimal

    def test_random_agreement(self):
        import random
        from app.algorithm import MeasuredSegment

        rng = random.Random(20261116)
        for _ in range(400):
            n = rng.randint(0, 4)
            m = rng.randint(0, 4)
            steps = []
            for i in range(n):
                start = rng.randint(0, 60)
                steps.append(
                    Step(i + 1, start, start + rng.randint(5, 30),
                         rng.choice([1, -1]))
                )
            segments = []
            for j in range(m):
                start = rng.randint(0, 60)
                segments.append(
                    MeasuredSegment(
                        j + 1, rng.choice([1, -1]), start,
                        start + rng.randint(5, 30), 0.0,
                    )
                )
            ordered_steps = sorted(steps, key=lambda s: (s.start_ts, s.end_ts, s.step_no))
            ordered_segs = sorted(segments, key=lambda s: (s.started_at, s.ended_at, s.segment_no))
            best_count, best_cost, optimal = self._brute(ordered_steps, ordered_segs)
            result = diagnose(steps, segments)
            self.assertEqual(result["max_pairs"], best_count)
            self.assertEqual(result["min_total_deviation"], best_cost)
            expected_candidates = len(optimal)
            self.assertEqual(result["candidate_count_exact"], expected_candidates)
            self.assertEqual(result["candidate_count"], expected_candidates)
            expected_definite = set.intersection(
                *(set(x) for x in optimal)
            ) if optimal else set()
            got_definite = {
                (p["step_no"], p["segment_no"]) for p in result["matched"]
            }
            self.assertEqual(
                got_definite,
                {
                    (ordered_steps[i].step_no, ordered_segs[j].segment_no)
                    for i, j in expected_definite
                },
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
