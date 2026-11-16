"""Charge/discharge cycle detection.

Input samples are (timestamp_utc_seconds:int, current_a:float) tuples,
already de-duplicated and sorted by timestamp for one cell.

Rules (see task statement):
  * zero current is a segment boundary.
  * only the continuous sequence zero -> positive -> zero -> negative -> zero
    forms a cycle.
  * a gap of more than 60 s between adjacent samples truncates the sequence.
  * capacity (Ah) is the per-interval integral using the value at the start
    of the interval (left value / forward rectangular rule).
  * unclosed sequences at the end are not counted.

The implementation runs in two passes:

1. tokenize: adjacent samples become INTERVAL tokens (with left-value
   integral, dropped when dt > 60 s); every sample also emits a ZERO or
   SIGN-FLIP boundary token. Intervals accumulate into PLUS/MINUS segments.
2. reduce the token stream with a small state machine that recognises
   ZERO, PLUS, ZERO, MINUS, ZERO.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

Sample = Tuple[int, float]

# token kinds
T_ZERO = "ZERO"
T_GAP = "GAP"
T_PLUS = "PLUS"
T_MINUS = "MINUS"
T_SIGNFLIP = "SIGNFLIP"

# state machine states
S_INIT = 0       # waiting for the opening zero
S_ZERO = 1       # saw opening zero
S_PLUS = 2       # saw positive (charge) segment
S_ZERO2 = 3      # saw zero after charge
S_MINUS = 4      # saw negative (discharge) segment

GAP_SECONDS = 60
EPS = 1e-12


@dataclass
class Token:
    kind: str
    ah: float = 0.0
    ts: int = 0  # timestamp of the boundary sample (for diagnostics)


@dataclass
class Cycle:
    cycle_no: int
    charge_ah: float
    discharge_ah: float
    started_at: int
    ended_at: int


def _is_zero(current: float) -> bool:
    return abs(current) <= EPS


def tokenize(samples: Sequence[Sample]) -> List[Token]:
    """Turn sorted samples into a boundary/segment token stream."""
    tokens: List[Token] = []
    if not samples:
        return tokens

    seg_sign = 0          # +1 / -1 while a non-zero segment is open, else 0
    seg_ah = 0.0

    def close_segment() -> None:
        nonlocal seg_sign, seg_ah
        if seg_sign == 1:
            tokens.append(Token(T_PLUS, seg_ah))
        elif seg_sign == -1:
            tokens.append(Token(T_MINUS, seg_ah))
        seg_sign = 0
        seg_ah = 0.0

    prev_ts: int | None = None
    prev_i: float | None = None

    for ts, current in samples:
        if prev_ts is not None:
            dt = ts - prev_ts
            if dt > GAP_SECONDS:
                # gap truncates: close any open segment and reset sequence
                close_segment()
                tokens.append(Token(T_GAP, ts=ts))
                prev_ts, prev_i = ts, current
                seg_sign = 0
                seg_ah = 0.0
                if _is_zero(current):
                    tokens.append(Token(T_ZERO, ts=ts))
                else:
                    seg_sign = 1 if current > 0 else -1
                continue

            # integral for [prev_ts, ts] using the previous (left) value
            if not _is_zero(prev_i):
                ah = abs(prev_i) * dt / 3600.0
                if seg_sign == 0:
                    seg_sign = 1 if prev_i > 0 else -1
                    seg_ah = ah
                elif (prev_i > 0) == (seg_sign == 1):
                    seg_ah += ah
                else:
                    # current sign flipped without a zero sample in between:
                    # close previous segment, emit a sign-flip boundary,
                    # open a new segment starting from this interval
                    close_segment()
                    tokens.append(Token(T_SIGNFLIP, ts=prev_ts))
                    seg_sign = 1 if prev_i > 0 else -1
                    seg_ah = ah

        if _is_zero(current):
            close_segment()
            tokens.append(Token(T_ZERO, ts=ts))
        prev_ts, prev_i = ts, current

    close_segment()
    return tokens


def detect_cycles(samples: Sequence[Sample]) -> List[Cycle]:
    """Detect closed charge/discharge cycles in sorted samples."""
    tokens = tokenize(samples)
    cycles: List[Cycle] = []

    state = S_INIT
    charge_ah = 0.0
    discharge_ah = 0.0
    started_at = 0
    last_zero_ts = 0

    def reset(ts: int) -> None:
        nonlocal state, charge_ah, discharge_ah, started_at
        state = S_ZERO
        charge_ah = 0.0
        discharge_ah = 0.0
        started_at = ts

    for tok in tokens:
        kind = tok.kind
        if kind == T_GAP:
            state = S_INIT
            continue
        if kind == T_ZERO:
            last_zero_ts = tok.ts
            if state == S_INIT:
                reset(tok.ts)
            elif state == S_PLUS:
                state = S_ZERO2
            elif state == S_MINUS:
                # closed cycle: ZERO PLUS ZERO MINUS ZERO
                cycles.append(
                    Cycle(
                        cycle_no=len(cycles) + 1,
                        charge_ah=charge_ah,
                        discharge_ah=discharge_ah,
                        started_at=started_at,
                        ended_at=tok.ts,
                    )
                )
                # the closing zero is also the opening zero of the next one
                reset(tok.ts)
            # ZERO while already in S_ZERO/S_ZERO2: stay put
            continue
        if kind == T_PLUS:
            if state == S_ZERO:
                state = S_PLUS
                charge_ah = tok.ah
            elif state == S_PLUS:
                charge_ah += tok.ah
            else:
                # positive segment in an unexpected place: restart from here
                # only once a zero arrives; go back to waiting
                state = S_INIT
            continue
        if kind == T_MINUS:
            if state == S_ZERO2:
                state = S_MINUS
                discharge_ah = tok.ah
            elif state == S_MINUS:
                discharge_ah += tok.ah
            else:
                state = S_INIT
            continue
        if kind == T_SIGNFLIP:
            # direct sign change without zero: current sequence is invalid
            state = S_INIT
            continue

    return cycles
