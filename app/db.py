"""SQLite persistence with transactional, versioned cell reports."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterable, List, Optional, Sequence, Tuple

from .algorithm import detect_cycles, extract_measured_segments
from .diagnostics import Step, diagnose

SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
    cell_id      TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS samples (
    cell_id      TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    current_ma   INTEGER NOT NULL,
    PRIMARY KEY (cell_id, ts)
);

CREATE TABLE IF NOT EXISTS report_versions (
    cell_id      TEXT NOT NULL,
    version      INTEGER NOT NULL,
    total_cycles INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (cell_id, version)
);

CREATE TABLE IF NOT EXISTS cycles (
    cell_id       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    cycle_no      INTEGER NOT NULL,
    charge_ah     REAL NOT NULL,
    discharge_ah  REAL NOT NULL,
    started_at    INTEGER NOT NULL,
    ended_at      INTEGER NOT NULL,
    PRIMARY KEY (cell_id, version, cycle_no)
);

CREATE TABLE IF NOT EXISTS upload_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id      TEXT NOT NULL,
    status       TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS step_table_versions (
    cell_id       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    step_count    INTEGER NOT NULL,
    PRIMARY KEY (cell_id, version)
);

CREATE TABLE IF NOT EXISTS step_table_rows (
    cell_id      TEXT NOT NULL,
    version      INTEGER NOT NULL,
    step_no      INTEGER NOT NULL,
    start_ts     INTEGER NOT NULL,
    end_ts       INTEGER NOT NULL,
    polarity     INTEGER NOT NULL,
    PRIMARY KEY (cell_id, version, step_no)
);

CREATE TABLE IF NOT EXISTS diagnostics_versions (
    cell_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    report_version  INTEGER NOT NULL,
    step_version    INTEGER NOT NULL,
    samples_digest  TEXT NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (cell_id, version)
);
"""


def _samples_digest(samples: Sequence[Tuple[int, float]]) -> str:
    """Stable fingerprint of the exact sample set used for a diagnosis."""
    payload = json.dumps(
        [[int(ts), float(current)] for ts, current in samples],
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _safe_rollback(conn: sqlite3.Connection) -> None:
    """Roll back if a transaction is open; never mask the original error."""
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


class ConflictError(Exception):
    """Same (cell, ts) already exists with a different current value."""

    def __init__(self, ts: int, existing: float, incoming: float):
        self.ts = ts
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"conflict at ts={ts}: existing={existing}A incoming={incoming}A"
        )


class ImportError_(Exception):
    """Generic import failure (bad CSV payload etc.)."""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = _connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #
    def import_batch(
        self, cell_id: str, rows: Iterable[Tuple[int, float]]
    ) -> dict:
        """Atomically import a CSV batch.

        Returns a summary dict. Raises ConflictError / ImportError_ on
        rejection; in that case nothing is committed (old samples and the
        previous report stay untouched).
        """
        # Normalize + validate the payload before opening the transaction.
        incoming: list[tuple[int, float]] = []
        seen: dict[int, float] = {}
        try:
            for ts, current in rows:
                ts = int(ts)
                current = float(current)
                if ts < 0:
                    raise ValueError(f"negative timestamp {ts}")
                if not (current == current):  # NaN
                    raise ValueError("NaN current")
                if ts in seen and seen[ts] != current:
                    raise ImportError_(
                        f"batch itself contains conflicting values at ts={ts}"
                    )
                seen[ts] = current
                incoming.append((ts, current))
        except (TypeError, ValueError) as exc:
            raise ImportError_(f"invalid row: {exc}") from exc

        if not incoming:
            raise ImportError_("CSV contains no data rows")

        conn = _connect(self.db_path)
        try:
            # One writer at a time. BEGIN IMMEDIATE acquires the reserved
            # lock up front so concurrent uploads serialize instead of
            # hitting SQLITE_BUSY mid-transaction.
            conn.execute("BEGIN IMMEDIATE")

            # Reject whole batch on any (cell, ts) value mismatch.
            for ts, current in incoming:
                row = conn.execute(
                    "SELECT current_ma FROM samples WHERE cell_id=? AND ts=?",
                    (cell_id, ts),
                ).fetchone()
                if row is not None and row["current_ma"] != _to_storage(current):
                    raise ConflictError(ts, row["current_ma"] / 1000.0, current)

            inserted = 0
            duplicate = 0
            for ts, current in incoming:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO samples(cell_id, ts, current_ma)"
                    " VALUES(?,?,?)",
                    (cell_id, ts, _to_storage(current)),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    duplicate += 1

            conn.execute(
                "INSERT INTO cells(cell_id) VALUES(?) ON CONFLICT(cell_id)"
                " DO UPDATE SET updated_at="
                "strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (cell_id,),
            )

            # Recompute the report from the full committed sample set.
            samples = _load_samples(conn, cell_id)
            cycles = detect_cycles(samples)

            version_row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM report_versions"
                " WHERE cell_id=?",
                (cell_id,),
            ).fetchone()
            version = version_row["v"] + 1

            conn.execute(
                "INSERT INTO report_versions(cell_id, version, total_cycles)"
                " VALUES(?,?,?)",
                (cell_id, version, len(cycles)),
            )
            conn.executemany(
                "INSERT INTO cycles(cell_id, version, cycle_no, charge_ah,"
                " discharge_ah, started_at, ended_at) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        cell_id,
                        version,
                        c.cycle_no,
                        c.charge_ah,
                        c.discharge_ah,
                        c.started_at,
                        c.ended_at,
                    )
                    for c in cycles
                ],
            )
            # A late point can rewrite the diagnosis: recompute it against the
            # just-committed samples and the latest committed step table.  The
            # digest pins the exact sample set used.
            step_version = _latest_step_version(conn, cell_id)
            diagnostics_version = _write_diagnostics(
                conn, cell_id, version, step_version, samples
            )
            conn.execute(
                "INSERT INTO upload_log(cell_id, status, detail) VALUES(?,?,?)",
                (cell_id, "accepted", f"version={version}"),
            )
            conn.execute("COMMIT")
        except Exception:
            _safe_rollback(conn)
            try:
                with self._conn() as log_conn:
                    log_conn.execute(
                        "INSERT INTO upload_log(cell_id, status, detail)"
                        " VALUES(?,?,?)",
                        (cell_id, "rejected", "batch rejected; no data changed"),
                    )
            except Exception:
                pass
            raise
        finally:
            conn.close()

        return {
            "cell_id": cell_id,
            "inserted": inserted,
            "duplicate": duplicate,
            "version": version,
            "total_cycles": len(cycles),
            "diagnostics_version": diagnostics_version,
        }

    # ------------------------------------------------------------------ #
    # Step-table replacement
    # ------------------------------------------------------------------ #
    def replace_step_table(self, cell_id: str, steps: Iterable[Step]) -> dict:
        """Atomically replace the whole approved step table for one cell.

        The new table is written, and the diagnosis is recomputed against the
        current committed samples, inside one transaction.  On any failure
        the transaction rolls back and the previously committed table (and
        diagnosis) remain in force.
        """
        ordered = sorted(steps, key=lambda s: (s.start_ts, s.end_ts, s.step_no))
        if not ordered:
            raise ImportError_("step table contains no rows")
        seen: set[int] = set()
        for step in ordered:
            if step.step_no in seen:
                raise ImportError_(f"duplicate step number {step.step_no}")
            seen.add(step.step_no)
            if step.start_ts < 0 or step.end_ts < 0:
                raise ImportError_(
                    f"step {step.step_no}: timestamps must be non-negative"
                )
            if step.start_ts >= step.end_ts:
                raise ImportError_(
                    f"step {step.step_no}: start must be before end"
                )
            if step.polarity not in (1, -1):
                raise ImportError_(
                    f"step {step.step_no}: polarity must be +1 or -1"
                )
        for previous, current in zip(ordered, ordered[1:]):
            if current.start_ts < previous.end_ts:
                raise ImportError_(
                    "time ranges overlap: step "
                    f"{previous.step_no} and step {current.step_no}"
                )

        conn = _connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")

            conn.execute(
                "INSERT INTO cells(cell_id) VALUES(?) ON CONFLICT(cell_id)"
                " DO UPDATE SET updated_at="
                "strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (cell_id,),
            )
            version_row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM step_table_versions"
                " WHERE cell_id=?",
                (cell_id,),
            ).fetchone()
            step_version = version_row["v"] + 1
            conn.execute(
                "INSERT INTO step_table_versions(cell_id, version, step_count)"
                " VALUES(?,?,?)",
                (cell_id, step_version, len(ordered)),
            )
            conn.executemany(
                "INSERT INTO step_table_rows(cell_id, version, step_no,"
                " start_ts, end_ts, polarity) VALUES(?,?,?,?,?,?)",
                [
                    (
                        cell_id,
                        step_version,
                        step.step_no,
                        step.start_ts,
                        step.end_ts,
                        step.polarity,
                    )
                    for step in ordered
                ],
            )

            report_row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM report_versions"
                " WHERE cell_id=?",
                (cell_id,),
            ).fetchone()
            report_version = report_row["v"]
            samples = _load_samples(conn, cell_id)
            diagnostics_version = _write_diagnostics(
                conn, cell_id, report_version, step_version, samples
            )
            conn.execute(
                "INSERT INTO upload_log(cell_id, status, detail) VALUES(?,?,?)",
                (
                    cell_id,
                    "accepted",
                    f"step_table_version={step_version}",
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            _safe_rollback(conn)
            try:
                with self._conn() as log_conn:
                    log_conn.execute(
                        "INSERT INTO upload_log(cell_id, status, detail)"
                        " VALUES(?,?,?)",
                        (
                            cell_id,
                            "rejected",
                            "step table rejected; old table kept",
                        ),
                    )
            except Exception:
                pass
            raise
        finally:
            conn.close()

        return {
            "cell_id": cell_id,
            "step_version": step_version,
            "step_count": len(ordered),
            "report_version": report_version,
            "diagnostics_version": diagnostics_version,
        }

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def get_report(self, cell_id: str) -> Optional[dict]:
        """Return report + cycles of the latest committed version.

        The version row, cycle details and total are all read from the same
        committed version; a single snapshot transaction guarantees they
        belong to one state even if an upload commits concurrently.
        """
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                cell = conn.execute(
                    "SELECT cell_id FROM cells WHERE cell_id=?", (cell_id,)
                ).fetchone()
                if cell is None:
                    return None
                ver = conn.execute(
                    "SELECT version, total_cycles, created_at FROM"
                    " report_versions WHERE cell_id=? ORDER BY version DESC"
                    " LIMIT 1",
                    (cell_id,),
                ).fetchone()
                if ver is None:
                    return {
                        "cell_id": cell_id,
                        "version": 0,
                        "total_cycles": 0,
                        "cycles": [],
                        "created_at": None,
                    }
                cycle_rows = conn.execute(
                    "SELECT cycle_no, charge_ah, discharge_ah, started_at,"
                    " ended_at FROM cycles WHERE cell_id=? AND version=?"
                    " ORDER BY cycle_no ASC",
                    (cell_id, ver["version"]),
                ).fetchall()
                return {
                    "cell_id": cell_id,
                    "version": ver["version"],
                    "total_cycles": ver["total_cycles"],
                    "created_at": ver["created_at"],
                    "cycles": [dict(r) for r in cycle_rows],
                }
            finally:
                conn.execute("COMMIT")

    def list_cells(self) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT c.cell_id, c.created_at, c.updated_at,"
                " COALESCE(v.total_cycles,0) AS total_cycles,"
                " COALESCE(v.version,0) AS version,"
                " COALESCE((SELECT MAX(version) FROM step_table_versions st"
                "           WHERE st.cell_id=c.cell_id),0) AS step_version"
                " FROM cells c"
                " LEFT JOIN report_versions v"
                " ON v.cell_id=c.cell_id AND v.version="
                " (SELECT MAX(version) FROM report_versions WHERE cell_id=c.cell_id)"
                " ORDER BY c.cell_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_step_table(self, cell_id: str) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                cell = conn.execute(
                    "SELECT cell_id FROM cells WHERE cell_id=?", (cell_id,)
                ).fetchone()
                if cell is None:
                    return None
                ver = conn.execute(
                    "SELECT version, step_count, created_at FROM"
                    " step_table_versions WHERE cell_id=?"
                    " ORDER BY version DESC LIMIT 1",
                    (cell_id,),
                ).fetchone()
                if ver is None:
                    return {
                        "cell_id": cell_id,
                        "step_version": 0,
                        "step_count": 0,
                        "created_at": None,
                        "steps": [],
                    }
                rows = conn.execute(
                    "SELECT step_no, start_ts, end_ts, polarity FROM"
                    " step_table_rows WHERE cell_id=? AND version=?"
                    " ORDER BY start_ts, end_ts, step_no",
                    (cell_id, ver["version"]),
                ).fetchall()
                return {
                    "cell_id": cell_id,
                    "step_version": ver["version"],
                    "step_count": ver["step_count"],
                    "created_at": ver["created_at"],
                    "steps": [
                        {
                            "step_no": r["step_no"],
                            "start_ts": r["start_ts"],
                            "end_ts": r["end_ts"],
                            "polarity": (
                                "positive" if r["polarity"] > 0 else "negative"
                            ),
                            "polarity_sign": r["polarity"],
                        }
                        for r in rows
                    ],
                }
            finally:
                conn.execute("COMMIT")

    def get_diagnostics(self, cell_id: str) -> Optional[dict]:
        """Latest committed diagnosis, with its sample/report/step versions.

        The diagnosis payload, the version stamps and the sample digest are
        read in one snapshot transaction, so all of them belong to the same
        committed state.  The digest is recomputed from the samples visible
        in that snapshot; a mismatch is surfaced rather than hidden.
        """
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                cell = conn.execute(
                    "SELECT cell_id FROM cells WHERE cell_id=?", (cell_id,)
                ).fetchone()
                if cell is None:
                    return None
                row = conn.execute(
                    "SELECT version, report_version, step_version,"
                    " samples_digest, payload, created_at FROM"
                    " diagnostics_versions WHERE cell_id=?"
                    " ORDER BY version DESC LIMIT 1",
                    (cell_id,),
                ).fetchone()
                if row is None:
                    # Legacy database (samples/report exist but no diagnosis
                    # row was ever written).  Compute on demand from the same
                    # snapshot rather than inventing a stamp.
                    samples = _load_samples(conn, cell_id)
                    report_row = conn.execute(
                        "SELECT COALESCE(MAX(version),0) AS v FROM"
                        " report_versions WHERE cell_id=?",
                        (cell_id,),
                    ).fetchone()
                    step_version = _latest_step_version(conn, cell_id)
                    payload = _build_diagnostics(
                        _load_steps(conn, cell_id, step_version), samples
                    )
                    payload.update(
                        {
                            "cell_id": cell_id,
                            "diagnostics_version": 0,
                            "report_version": report_row["v"],
                            "step_version": step_version,
                            "samples_digest": _samples_digest(samples),
                            "current_samples_digest": _samples_digest(samples),
                            "consistent": True,
                            "created_at": None,
                            "step_table": _step_table_snapshot(
                                conn, cell_id, step_version
                            ),
                        }
                    )
                    return payload
                samples = _load_samples(conn, cell_id)
                current_digest = _samples_digest(samples)
                payload = json.loads(row["payload"])
                step_table = _step_table_snapshot(
                    conn, cell_id, row["step_version"]
                )
                report_row = conn.execute(
                    "SELECT COALESCE(MAX(version),0) AS v FROM report_versions"
                    " WHERE cell_id=?",
                    (cell_id,),
                ).fetchone()
                consistent = (
                    current_digest == row["samples_digest"]
                    and row["report_version"] == report_row["v"]
                    and row["step_version"] == _latest_step_version(conn, cell_id)
                )
                payload.update(
                    {
                        "cell_id": cell_id,
                        "diagnostics_version": row["version"],
                        "report_version": row["report_version"],
                        "step_version": row["step_version"],
                        "samples_digest": row["samples_digest"],
                        "current_samples_digest": current_digest,
                        "consistent": consistent,
                        "created_at": row["created_at"],
                        "step_table": step_table,
                    }
                )
                return payload
            finally:
                conn.execute("COMMIT")

    def get_state(self, cell_id: str) -> Optional[dict]:
        """Report, step table and diagnosis from one commit snapshot.

        Everything is read inside a single read transaction and stamped with
        the same report version / step version / diagnostics version and a
        digest of the exact samples.  ``consistent`` is true only when the
        stored diagnosis digest matches the samples visible in that same
        snapshot and the diagnosis pins the report version being returned.
        """
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                cell = conn.execute(
                    "SELECT cell_id FROM cells WHERE cell_id=?", (cell_id,)
                ).fetchone()
                if cell is None:
                    return None

                report_row = conn.execute(
                    "SELECT version, total_cycles, created_at FROM"
                    " report_versions WHERE cell_id=?"
                    " ORDER BY version DESC LIMIT 1",
                    (cell_id,),
                ).fetchone()
                report = {
                    "cell_id": cell_id,
                    "version": report_row["version"] if report_row else 0,
                    "total_cycles": (
                        report_row["total_cycles"] if report_row else 0
                    ),
                    "created_at": report_row["created_at"] if report_row else None,
                    "cycles": [],
                }
                if report_row is not None:
                    cycle_rows = conn.execute(
                        "SELECT cycle_no, charge_ah, discharge_ah, started_at,"
                        " ended_at FROM cycles WHERE cell_id=? AND version=?"
                        " ORDER BY cycle_no ASC",
                        (cell_id, report_row["version"]),
                    ).fetchall()
                    report["cycles"] = [dict(r) for r in cycle_rows]

                step_version = _latest_step_version(conn, cell_id)
                step_table = _step_table_snapshot(conn, cell_id, step_version)

                diag_row = conn.execute(
                    "SELECT version, report_version, step_version,"
                    " samples_digest, payload, created_at FROM"
                    " diagnostics_versions WHERE cell_id=?"
                    " ORDER BY version DESC LIMIT 1",
                    (cell_id,),
                ).fetchone()
                if diag_row is None:
                    samples = _load_samples(conn, cell_id)
                    current_digest = _samples_digest(samples)
                    diagnostics = _build_diagnostics(
                        _load_steps(conn, cell_id, step_version), samples
                    )
                    diagnostics.update(
                        {
                            "cell_id": cell_id,
                            "diagnostics_version": 0,
                            "report_version": report["version"],
                            "step_version": step_version,
                            "samples_digest": current_digest,
                            "current_samples_digest": current_digest,
                            "consistent": True,
                            "created_at": None,
                        }
                    )
                else:
                    samples = _load_samples(conn, cell_id)
                    current_digest = _samples_digest(samples)
                    diagnostics = json.loads(diag_row["payload"])
                    consistent = (
                        current_digest == diag_row["samples_digest"]
                        and diag_row["report_version"] == report["version"]
                        and diag_row["step_version"] == step_version
                    )
                    diagnostics.update(
                        {
                            "diagnostics_version": diag_row["version"],
                            "report_version": diag_row["report_version"],
                            "step_version": diag_row["step_version"],
                            "samples_digest": diag_row["samples_digest"],
                            "current_samples_digest": current_digest,
                            "consistent": consistent,
                            "created_at": diag_row["created_at"],
                        }
                    )
                diagnostics["step_table"] = step_table
                diagnostics["cell_id"] = cell_id
                return {
                    "cell_id": cell_id,
                    "report": report,
                    "step_table": step_table,
                    "diagnostics": diagnostics,
                }
            finally:
                conn.execute("COMMIT")


def _to_storage(current_a: float) -> int:
    """Store amperes as integer microamps.

    CSV currents like 1.234 A are stored as integer microamps so equality
    checks for idempotency are exact (no float text round-trip surprises).
    Precision below 1 µA is rejected by rounding consistency on read.
    """
    return round(float(current_a) * 1_000_000)


def _load_samples(
    conn: sqlite3.Connection, cell_id: str
) -> List[Tuple[int, float]]:
    rows = conn.execute(
        "SELECT ts, current_ma FROM samples WHERE cell_id=? ORDER BY ts ASC",
        (cell_id,),
    ).fetchall()
    return [(r["ts"], r["current_ma"] / 1_000_000.0) for r in rows]


def _latest_step_version(conn: sqlite3.Connection, cell_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM step_table_versions"
        " WHERE cell_id=?",
        (cell_id,),
    ).fetchone()
    return row["v"]


def _load_steps(
    conn: sqlite3.Connection, cell_id: str, step_version: int
) -> List[Step]:
    if step_version <= 0:
        return []
    rows = conn.execute(
        "SELECT step_no, start_ts, end_ts, polarity FROM step_table_rows"
        " WHERE cell_id=? AND version=? ORDER BY start_ts, end_ts, step_no",
        (cell_id, step_version),
    ).fetchall()
    return [
        Step(
            step_no=r["step_no"],
            start_ts=r["start_ts"],
            end_ts=r["end_ts"],
            polarity=r["polarity"],
        )
        for r in rows
    ]


def _build_diagnostics(
    steps: Sequence[Step], samples: Sequence[Tuple[int, float]]
) -> dict:
    """Compute the diagnosis payload (without version/stamp fields)."""
    segments = extract_measured_segments(samples)
    payload = diagnose(steps, segments)
    payload["segments"] = [
        {
            "segment_no": s.segment_no,
            "polarity": "positive" if s.polarity > 0 else "negative",
            "polarity_sign": s.polarity,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "capacity_ah": s.capacity_ah,
        }
        for s in segments
    ]
    return payload


def _write_diagnostics(
    conn: sqlite3.Connection,
    cell_id: str,
    report_version: int,
    step_version: int,
    samples: Sequence[Tuple[int, float]],
) -> int:
    """Compute and append one diagnosis row; return its version."""
    steps = _load_steps(conn, cell_id, step_version)
    payload = _build_diagnostics(steps, samples)
    row = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM diagnostics_versions"
        " WHERE cell_id=?",
        (cell_id,),
    ).fetchone()
    version = row["v"] + 1
    conn.execute(
        "INSERT INTO diagnostics_versions(cell_id, version, report_version,"
        " step_version, samples_digest, payload) VALUES(?,?,?,?,?,?)",
        (
            cell_id,
            version,
            report_version,
            step_version,
            _samples_digest(samples),
            json.dumps(payload, sort_keys=True),
        ),
    )
    return version


def _step_table_snapshot(
    conn: sqlite3.Connection, cell_id: str, step_version: int
) -> Optional[dict]:
    if step_version <= 0:
        return None
    row = conn.execute(
        "SELECT created_at FROM step_table_versions"
        " WHERE cell_id=? AND version=?",
        (cell_id, step_version),
    ).fetchone()
    steps = _load_steps(conn, cell_id, step_version)
    return {
        "cell_id": cell_id,
        "step_version": step_version,
        "step_count": len(steps),
        "created_at": row["created_at"] if row is not None else None,
        "steps": [
            {
                "step_no": s.step_no,
                "start_ts": s.start_ts,
                "end_ts": s.end_ts,
                "polarity": "positive" if s.polarity > 0 else "negative",
                "polarity_sign": s.polarity,
            }
            for s in steps
        ],
    }
