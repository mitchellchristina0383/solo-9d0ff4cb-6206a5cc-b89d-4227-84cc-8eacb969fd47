"""Match an approved step table against measured current segments.

The step table is a chronologically ordered list of :class:`Step` rows
(step number, start/end UTC seconds, polarity).  Measured segments come from
:func:`app.algorithm.extract_measured_segments`.

Pairing rules
-------------
* one-to-one and order preserving (a matching may not cross);
* only steps and segments with the same polarity may be paired;
* both the start and the end must be within ``TOLERANCE_SECONDS`` (15 s);
* first maximise the number of pairs, then minimise the sum of the absolute
  start/end deviations.

The optimisation is a suffix DP over ``(step prefix, segment prefix)``.  All
optimal matchings are counted exactly, so a pair is reported as *definite*
only when it occurs in **every** optimal matching.  When several optima
exist, the complete set is enumerated (up to safety caps) and returned as
candidates instead of silently choosing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .algorithm import MeasuredSegment

TOLERANCE_SECONDS = 15

# Safety valves for pathological inputs with a huge number of tied optima.
MAX_CANDIDATES = 1000
MAX_SEARCH_NODES = 200_000


@dataclass(frozen=True)
class Step:
    step_no: int
    start_ts: int
    end_ts: int
    polarity: int  # +1 charge, -1 discharge


def polarity_name(polarity: int) -> str:
    return "positive" if polarity > 0 else "negative"


def _deviations(step: Step, segment: MeasuredSegment) -> Tuple[int, int]:
    return (
        abs(step.start_ts - segment.started_at),
        abs(step.end_ts - segment.ended_at),
    )


def _compatible(step: Step, segment: MeasuredSegment) -> bool:
    if step.polarity != segment.polarity:
        return False
    start_dev, end_dev = _deviations(step, segment)
    return start_dev <= TOLERANCE_SECONDS and end_dev <= TOLERANCE_SECONDS


def _step_dict(step: Step) -> Dict:
    return {
        "step_no": step.step_no,
        "polarity": polarity_name(step.polarity),
        "polarity_sign": step.polarity,
        "start_ts": step.start_ts,
        "end_ts": step.end_ts,
    }


def _segment_dict(segment: MeasuredSegment) -> Dict:
    return {
        "segment_no": segment.segment_no,
        "polarity": polarity_name(segment.polarity),
        "polarity_sign": segment.polarity,
        "started_at": segment.started_at,
        "ended_at": segment.ended_at,
        "capacity_ah": segment.capacity_ah,
    }


def _pair_dict(step: Step, segment: MeasuredSegment) -> Dict:
    start_dev, end_dev = _deviations(step, segment)
    return {
        "step_no": step.step_no,
        "segment_no": segment.segment_no,
        "polarity": polarity_name(step.polarity),
        "polarity_sign": step.polarity,
        "step_start": step.start_ts,
        "step_end": step.end_ts,
        "measured_start": segment.started_at,
        "measured_end": segment.ended_at,
        "capacity_ah": segment.capacity_ah,
        "start_deviation_s": start_dev,
        "end_deviation_s": end_dev,
    }


def _better(count: int, cost: int, best_count: int, best_cost: int) -> bool:
    return count > best_count or (count == best_count and cost < best_cost)


def _solve(
    steps: Sequence[Step], segments: Sequence[MeasuredSegment]
) -> Dict:
    """Exact optimum, definite pairs and all optimal matchings."""
    n, m = len(steps), len(segments)

    # edges[i][j] = total start+end deviation, or None when inadmissible.
    edges: List[List[int | None]] = [
        [
            None
            if not _compatible(step, segment)
            else sum(_deviations(step, segment))
            for segment in segments
        ]
        for step in steps
    ]

    # Suffix DP over steps[i:] and segments[j:].
    #   f_c[i][j] = maximum number of pairs
    #   f_d[i][j] = minimum total deviation among matchings with f_c pairs
    f_c = [[0] * (m + 1) for _ in range(n + 1)]
    f_d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        f_c_i, f_c_next = f_c[i], f_c[i + 1]
        f_d_i, f_d_next = f_d[i], f_d[i + 1]
        edge_i = edges[i]
        for j in range(m - 1, -1, -1):
            best_count = f_c_next[j]
            best_cost = f_d_next[j]
            if _better(f_c_i[j + 1], f_d_i[j + 1], best_count, best_cost):
                best_count, best_cost = f_c_i[j + 1], f_d_i[j + 1]
            deviation = edge_i[j]
            if deviation is not None:
                count = f_c_next[j + 1] + 1
                cost = f_d_next[j + 1] + deviation
                if _better(count, cost, best_count, best_cost):
                    best_count, best_cost = count, cost
            f_c_i[j] = best_count
            f_d_i[j] = best_cost

    max_pairs = f_c[0][0]
    min_cost = f_d[0][0]

    # ways[i][j] = number of optimal matchings of steps[i:] using segments
    # from j onwards.  For state (i, j), step i is either unmatched, or paired
    # with some j2 >= j (segments j..j2-1 are then skipped).  A bucket keyed
    # by the resulting (count, cost) keeps the suffix sum over j2 >= j.
    ways = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        # No steps left or no segments left: the only matching is the empty
        # one over the remainder, and it is optimal (zero pairs, zero cost).
        ways[i][m] = 1
    for j in range(m):
        ways[n][j] = 1
    for i in range(n - 1, -1, -1):
        ways_i, ways_next = ways[i], ways[i + 1]
        f_c_i, f_d_i = f_c[i], f_d[i]
        f_c_next, f_d_next = f_c[i + 1], f_d[i + 1]
        edge_i = edges[i]
        pair_buckets: Dict[Tuple[int, int], int] = {}
        for j in range(m - 1, -1, -1):
            deviation = edge_i[j]
            if deviation is not None:
                key = (f_c_next[j + 1] + 1, f_d_next[j + 1] + deviation)
                pair_buckets[key] = pair_buckets.get(key, 0) + ways_next[j + 1]
            total = 0
            if f_c_next[j] == f_c_i[j] and f_d_next[j] == f_d_i[j]:
                total += ways_next[j]
            total += pair_buckets.get((f_c_i[j], f_d_i[j]), 0)
            ways_i[j] = total
    total_optimal = ways[0][0]

    # pre[i][j] = number of optimal ways to resolve steps[:i] such that the
    # segment cursor is at j (segments < j are consumed: matched or skipped)
    # and the remaining optimum f[i][j] still reaches the global optimum.
    # pair_entry[i][j] = optimal prefixes that pair step i with segment j
    # (the cursor may have been at any j0 <= j).
    pre = [[0] * (m + 1) for _ in range(n + 1)]
    pre[0][0] = 1
    pair_entry: Dict[Tuple[int, int], int] = {}
    for i in range(n):
        pre_i, pre_next = pre[i], pre[i + 1]
        f_c_i, f_d_i = f_c[i], f_d[i]
        f_c_next, f_d_next = f_c[i + 1], f_d[i + 1]
        edge_i = edges[i]
        state_buckets: Dict[Tuple[int, int], int] = {}
        for j in range(m + 1):
            base = pre_i[j]
            if base:
                key = (f_c_i[j], f_d_i[j])
                state_buckets[key] = state_buckets.get(key, 0) + base
                # Step i unmatched keeps the segment cursor at j.
                if f_c_next[j] == f_c_i[j] and f_d_next[j] == f_d_i[j]:
                    pre_next[j] += base
            if j == m:
                continue
            deviation = edge_i[j]
            if deviation is None:
                continue
            # States with cursor j0 <= j whose target equals this pair's
            # value may pair step i with segment j.
            key = (f_c_next[j + 1] + 1, f_d_next[j + 1] + deviation)
            entry = state_buckets.get(key, 0)
            if entry:
                pair_entry[(i, j)] = entry
                pre_next[j + 1] += entry

    # pair_count[i][j] = number of optimal matchings containing (i, j).
    possible_pairs: set[Tuple[int, int]] = set()
    definite_pairs: set[Tuple[int, int]] = set()
    pair_counts: Dict[Tuple[int, int], int] = {}
    for (i, j), entry in pair_entry.items():
        count = entry * ways[i + 1][j + 1]
        if count <= 0:
            continue
        possible_pairs.add((i, j))
        pair_counts[(i, j)] = count
        if count == total_optimal:
            definite_pairs.add((i, j))

    # Exact count of optimal matchings in which a step is unmatched.
    unmatched_step_counts: Dict[int, int] = {}
    for i in range(n):
        total = 0
        for j in range(m + 1):
            if not pre[i][j]:
                continue
            if f_c[i + 1][j] == f_c[i][j] and f_d[i + 1][j] == f_d[i][j]:
                total += pre[i][j] * ways[i + 1][j]
        if total:
            unmatched_step_counts[i] = total

    # Exact count of optimal matchings in which a segment is unmatched.
    segment_used: Dict[int, int] = {}
    for (_, j), count in pair_counts.items():
        segment_used[j] = segment_used.get(j, 0) + count
    unmatched_segment_counts = {
        j: total_optimal - segment_used.get(j, 0)
        for j in range(m)
        if total_optimal - segment_used.get(j, 0) > 0
    }

    matchings, truncated = _enumerate_matchings(steps, edges, f_c, f_d)

    return {
        "max_pairs": max_pairs,
        "min_cost": min_cost,
        "total_optimal": total_optimal,
        "possible_pairs": possible_pairs,
        "definite_pairs": definite_pairs,
        "unmatched_step_counts": unmatched_step_counts,
        "unmatched_segment_counts": unmatched_segment_counts,
        "matchings": matchings,
        "truncated": truncated,
    }


def _enumerate_matchings(
    steps: Sequence[Step],
    edges: List[List[int | None]],
    f_c: List[List[int]],
    f_d: List[List[int]],
) -> Tuple[List[Tuple[Tuple[int, int], ...]], bool]:
    """Enumerate optimal matchings; each matching appears exactly once."""
    n, m = len(steps), len(edges[0]) if edges else 0
    results: List[Tuple[Tuple[int, int], ...]] = []
    truncated = False
    nodes = 0
    # (step index, next available segment index, chosen pairs)
    stack: List[Tuple[int, int, Tuple[Tuple[int, int], ...]]] = [(0, 0, ())]
    while stack:
        if len(results) >= MAX_CANDIDATES or nodes >= MAX_SEARCH_NODES:
            truncated = True
            break
        nodes += 1
        i, j, pairs = stack.pop()
        if i == n:
            results.append(pairs)
            continue
        f_c_i, f_d_i = f_c[i], f_d[i]
        f_c_next, f_d_next = f_c[i + 1], f_d[i + 1]
        if f_c_next[j] == f_c_i[j] and f_d_next[j] == f_d_i[j]:
            stack.append((i + 1, j, pairs))
        edge_i = edges[i]
        for j2 in range(m - 1, j - 1, -1):
            deviation = edge_i[j2]
            if deviation is None:
                continue
            if f_c_next[j2 + 1] + 1 != f_c_i[j]:
                continue
            if f_d_next[j2 + 1] + deviation != f_d_i[j]:
                continue
            stack.append((i + 1, j2 + 1, pairs + ((i, j2),)))

    results.sort(
        key=lambda pairs: tuple(
            (steps[i].step_no, j) for i, j in pairs
        )
    )
    return results, truncated


def diagnose(
    steps: Sequence[Step], segments: Sequence[MeasuredSegment]
) -> Dict:
    """Compute the full, JSON-serialisable diagnostics payload.

    ``matched`` contains only pairs present in every optimal matching.
    Steps/segments that are not definitely matched but can take part in an
    optimum are listed under ``ambiguous_steps`` / ``ambiguous_segments`` with
    their candidate counterparts; those that take part in no optimum are
    listed as unmatched.  ``candidates`` contains the enumerated optimal
    matchings (exactly one when the optimum is unique).
    """
    ordered_steps = sorted(steps, key=lambda s: (s.start_ts, s.end_ts, s.step_no))
    ordered_segments = sorted(
        segments, key=lambda s: (s.started_at, s.ended_at, s.segment_no)
    )

    solved = _solve(ordered_steps, ordered_segments)
    possible_pairs = solved["possible_pairs"]
    definite_pairs = solved["definite_pairs"]
    total_optimal = solved["total_optimal"]

    step_choices: Dict[int, set] = {}
    segment_choices: Dict[int, set] = {}
    for i, j in possible_pairs:
        step_choices.setdefault(i, set()).add(j)
        segment_choices.setdefault(j, set()).add(i)

    candidates = []
    for matching in solved["matchings"]:
        matched_steps = {i for i, _ in matching}
        matched_segments = {j for _, j in matching}
        candidates.append(
            {
                "pairs": [
                    _pair_dict(ordered_steps[i], ordered_segments[j])
                    for i, j in matching
                ],
                "unmatched_step_nos": sorted(
                    ordered_steps[i].step_no
                    for i in range(len(ordered_steps))
                    if i not in matched_steps
                ),
                "unmatched_segment_nos": [
                    ordered_segments[j].segment_no
                    for j in range(len(ordered_segments))
                    if j not in matched_segments
                ],
            }
        )

    matched_out = [
        _pair_dict(ordered_steps[i], ordered_segments[j])
        for i, j in sorted(definite_pairs)
    ]

    unmatched_step_counts = solved["unmatched_step_counts"]
    unmatched_segment_counts = solved["unmatched_segment_counts"]

    ambiguous_steps = []
    for i in sorted(step_choices):
        if i in {si for si, _ in definite_pairs}:
            continue
        item = _step_dict(ordered_steps[i])
        item["candidate_segment_nos"] = sorted(
            ordered_segments[j].segment_no for j in step_choices[i]
        )
        item["may_be_unmatched"] = i in unmatched_step_counts
        ambiguous_steps.append(item)

    ambiguous_segments = []
    for j in sorted(segment_choices):
        if j in {sj for _, sj in definite_pairs}:
            continue
        item = _segment_dict(ordered_segments[j])
        item["candidate_step_nos"] = sorted(
            ordered_steps[i].step_no for i in segment_choices[j]
        )
        item["may_be_unmatched"] = j in unmatched_segment_counts
        ambiguous_segments.append(item)

    unmatched_steps = sorted(
        (
            _step_dict(ordered_steps[i])
            for i in range(len(ordered_steps))
            if i not in step_choices
        ),
        key=lambda x: x["step_no"],
    )
    unmatched_segments = [
        _segment_dict(ordered_segments[j])
        for j in range(len(ordered_segments))
        if j not in segment_choices
    ]

    return {
        "max_pairs": solved["max_pairs"],
        "min_total_deviation": solved["min_cost"],
        "ambiguous": total_optimal > 1,
        "candidate_count": len(solved["matchings"]),
        "candidate_count_exact": total_optimal,
        "candidates_truncated": solved["truncated"],
        "matched": matched_out,
        "unmatched_steps": unmatched_steps,
        "unmatched_segments": unmatched_segments,
        "ambiguous_steps": ambiguous_steps,
        "ambiguous_segments": ambiguous_segments,
        "candidates": candidates,
    }
