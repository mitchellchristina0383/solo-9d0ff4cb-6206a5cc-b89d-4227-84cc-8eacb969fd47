"""CSV parsing for current samples.

Accepted headers (case-insensitive, underscores/spaces interchangeable):
  timestamp / ts / timestamp_utc / timestamp_seconds / utc_seconds
  current / current_a / current_amp / i / amps
"""

from __future__ import annotations

import csv
import io
from typing import List, Tuple

TS_ALIASES = {
    "timestamp",
    "ts",
    "timestamp_utc",
    "timestamp_seconds",
    "utc_seconds",
    "time_s",
    "time",
}
CUR_ALIASES = {
    "current",
    "current_a",
    "current_amp",
    "current_amps",
    "i",
    "amps",
    "ampere",
    "amperes",
}


class CSVParseError(ValueError):
    pass


def _norm(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def parse_samples_csv(content: bytes | str) -> List[Tuple[int, float]]:
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CSVParseError(f"CSV must be UTF-8 encoded: {exc}") from exc
    else:
        text = content

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CSVParseError("empty CSV file")

    cols = [_norm(h) for h in header]
    try:
        ts_idx = next(i for i, c in enumerate(cols) if c in TS_ALIASES)
    except StopIteration:
        raise CSVParseError(
            f"missing timestamp column (accepted: {sorted(TS_ALIASES)})"
        )
    try:
        cur_idx = next(i for i, c in enumerate(cols) if c in CUR_ALIASES)
    except StopIteration:
        raise CSVParseError(
            f"missing current column (accepted: {sorted(CUR_ALIASES)})"
        )

    rows: List[Tuple[int, float]] = []
    for line_no, raw in enumerate(reader, start=2):
        if not raw or all(not c.strip() for c in raw):
            continue  # blank line
        if len(raw) <= max(ts_idx, cur_idx):
            raise CSVParseError(f"line {line_no}: expected at least 2 columns")
        ts_text = raw[ts_idx].strip()
        cur_text = raw[cur_idx].strip()
        try:
            ts = int(float(ts_text)) if "." in ts_text else int(ts_text)
        except ValueError as exc:
            raise CSVParseError(
                f"line {line_no}: timestamp {ts_text!r} is not an integer"
            ) from exc
        try:
            current = float(cur_text)
        except ValueError as exc:
            raise CSVParseError(
                f"line {line_no}: current {cur_text!r} is not a number"
            ) from exc
        rows.append((ts, current))

    if not rows:
        raise CSVParseError("CSV contains a header but no data rows")
    return rows
