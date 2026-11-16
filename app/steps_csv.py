"""CSV parsing and validation for approved charge/discharge step tables.

A step-table CSV must contain, per row:

  * step number,
  * start UTC second,
  * end UTC second,
  * current polarity (positive/charge or negative/discharge).

Columns are matched case-insensitively; underscores, spaces and hyphens are
interchangeable.  English and common Chinese header aliases are accepted.

Validation rules: integer non-negative timestamps, ``start < end``, unique
step numbers, and no overlapping time ranges.  Touching ranges (one step ends
exactly when the next starts) are allowed.  Any error rejects the whole file
so the previously committed table stays in force.
"""

from __future__ import annotations

import csv
import io
from typing import List

from .diagnostics import Step

STEP_ALIASES = {
    "step_no",
    "step",
    "step_number",
    "step_num",
    "stepno",
    "no",
    "number",
    "seq",
    "sequence",
    "工步号",
    "工步",
    "步骤",
    "步骤号",
    "序号",
}
START_ALIASES = {
    "start",
    "start_ts",
    "start_utc",
    "start_utc_s",
    "start_utc_seconds",
    "start_time",
    "start_seconds",
    "start_s",
    "startsec",
    "begin",
    "begin_ts",
    "utc_start",
    "起始",
    "起始秒",
    "起始时间",
    "起始utc秒",
    "开始",
    "开始时间",
    "开始utc秒",
}
END_ALIASES = {
    "end",
    "end_ts",
    "end_utc",
    "end_utc_s",
    "end_utc_seconds",
    "end_time",
    "end_seconds",
    "end_s",
    "endsec",
    "finish",
    "finish_ts",
    "utc_end",
    "结束",
    "结束秒",
    "结束时间",
    "结束utc秒",
    "终止",
    "终止时间",
    "止",
}
POLARITY_ALIASES = {
    "polarity",
    "current_polarity",
    "polarity_sign",
    "current_sign",
    "direction",
    "sign",
    "极性",
    "方向",
    "电流极性",
    "正负",
    "正负极",
}

POSITIVE_VALUES = {
    "positive",
    "pos",
    "p",
    "+",
    "1",
    "+1",
    "charge",
    "charging",
    "chg",
    "正",
    "正电流",
    "正向",
    "充电",
    "充",
}
NEGATIVE_VALUES = {
    "negative",
    "neg",
    "n",
    "-",
    "-1",
    "discharge",
    "discharging",
    "dis",
    "负",
    "负电流",
    "反向",
    "放电",
    "放",
}


class StepTableError(ValueError):
    """Invalid step-table CSV; the old table must remain in force."""


def _norm(name: str) -> str:
    # Underscores, spaces and hyphens are interchangeable, so remove them all;
    # this also makes "起始 UTC 秒" and "start_utc_seconds" match their aliases.
    cleaned = name.strip().lower().replace(" ", "_").replace("-", "_")
    return cleaned.replace("_", "")


STEP_ALIASES_NORM = {_norm(alias) for alias in STEP_ALIASES}
START_ALIASES_NORM = {_norm(alias) for alias in START_ALIASES}
END_ALIASES_NORM = {_norm(alias) for alias in END_ALIASES}
POLARITY_ALIASES_NORM = {_norm(alias) for alias in POLARITY_ALIASES}


def _find_column(cols: List[str], aliases: set, label: str) -> int:
    try:
        return next(i for i, c in enumerate(cols) if c in aliases)
    except StopIteration:
        raise StepTableError(
            f"missing {label} column (accepted: {sorted(aliases)})"
        ) from None


def _parse_int(text: str, field: str, line_no: int) -> int:
    value = text.strip()
    if not value:
        raise StepTableError(f"line {line_no}: empty {field}")
    try:
        return int(value)
    except ValueError:
        pass
    try:
        number = float(value)
    except ValueError:
        raise StepTableError(
            f"line {line_no}: {field} {value!r} is not an integer"
        ) from None
    if not number.is_integer():
        raise StepTableError(
            f"line {line_no}: {field} {value!r} is not an integer"
        )
    return int(number)


def _parse_polarity(text: str, line_no: int) -> int:
    value = text.strip().lower()
    if value in POSITIVE_VALUES:
        return 1
    if value in NEGATIVE_VALUES:
        return -1
    raise StepTableError(
        f"line {line_no}: polarity {text!r} must be positive/negative"
        " (+/-1, charge/discharge, 正/负)"
    )


def parse_steps_csv(content: bytes | str) -> List[Step]:
    """Parse and validate a whole step table, sorted by start time."""
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise StepTableError(f"CSV must be UTF-8 encoded: {exc}") from exc
    else:
        text = content

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise StepTableError("empty CSV file") from None

    cols = [_norm(h) for h in header]
    step_idx = _find_column(cols, STEP_ALIASES_NORM, "step number")
    start_idx = _find_column(cols, START_ALIASES_NORM, "start UTC second")
    end_idx = _find_column(cols, END_ALIASES_NORM, "end UTC second")
    polarity_idx = _find_column(cols, POLARITY_ALIASES_NORM, "polarity")
    needed = max(step_idx, start_idx, end_idx, polarity_idx)

    steps: List[Step] = []
    seen_step_nos: set[int] = set()
    for line_no, raw in enumerate(reader, start=2):
        if not raw or all(not c.strip() for c in raw):
            continue
        if len(raw) <= needed:
            raise StepTableError(
                f"line {line_no}: expected at least {needed + 1} columns"
            )
        step_no = _parse_int(raw[step_idx], "step number", line_no)
        start_ts = _parse_int(raw[start_idx], "start UTC second", line_no)
        end_ts = _parse_int(raw[end_idx], "end UTC second", line_no)
        polarity = _parse_polarity(raw[polarity_idx], line_no)

        if step_no in seen_step_nos:
            raise StepTableError(f"line {line_no}: duplicate step number {step_no}")
        seen_step_nos.add(step_no)
        if start_ts < 0 or end_ts < 0:
            raise StepTableError(
                f"line {line_no}: timestamps must be non-negative"
            )
        if start_ts >= end_ts:
            raise StepTableError(
                f"line {line_no}: start {start_ts} must be before end {end_ts}"
            )
        steps.append(
            Step(
                step_no=step_no,
                start_ts=start_ts,
                end_ts=end_ts,
                polarity=polarity,
            )
        )

    if not steps:
        raise StepTableError("CSV contains a header but no data rows")

    ordered = sorted(steps, key=lambda s: (s.start_ts, s.end_ts, s.step_no))
    for previous, current in zip(ordered, ordered[1:]):
        if current.start_ts < previous.end_ts:
            raise StepTableError(
                "time ranges overlap: step "
                f"{previous.step_no} [{previous.start_ts},{previous.end_ts}] and"
                f" step {current.step_no} [{current.start_ts},{current.end_ts}]"
            )

    return ordered
