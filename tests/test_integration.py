"""End-to-end HTTP tests: upload, idempotency, late points, gaps, concurrency."""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="battery-test-")
_DB = os.path.join(_TMP, "test.db")
for _suffix in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _suffix)
    except FileNotFoundError:
        pass
os.environ["BATTERY_DB_PATH"] = _DB

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def csv_text(rows, header="timestamp,current\n"):
    return header + "".join(f"{ts},{cur}\n" for ts, cur in rows)


def upload(client, cell_id, rows, header="timestamp,current\n"):
    return client.post(
        f"/cells/{cell_id}/samples",
        files={"file": ("samples.csv", csv_text(rows, header), "text/csv")},
    )


class UploadReportTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)

    def test_full_cycle_capacity_and_report(self):
        cell = "CELL-E2E-1"
        rows = [
            (0, 0.0),
            (10, 2.0),
            (20, 0.0),
            (30, -3.0),
            (40, 0.0),
        ]
        r = upload(self.c, cell, rows)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["inserted"], 5)
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["total_cycles"], 1)

        rep = self.c.get(f"/cells/{cell}/report").json()
        self.assertEqual(rep["version"], 1)
        self.assertEqual(rep["total_cycles"], 1)
        self.assertEqual(len(rep["cycles"]), 1)
        cyc = rep["cycles"][0]
        self.assertAlmostEqual(cyc["charge_ah"], 2.0 * 10 / 3600)
        self.assertAlmostEqual(cyc["discharge_ah"], 3.0 * 10 / 3600)

    def test_alternative_header_names(self):
        cell = "CELL-E2E-HDR"
        r = upload(
            self.c,
            cell,
            [(0, 0), (5, 1), (10, 0), (15, -1), (20, 0)],
            header="UTC Seconds,Current A\n",
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total_cycles"], 1)

    def test_bad_csv_keeps_old_data(self):
        cell = "CELL-E2E-BAD"
        r = upload(self.c, cell, [(0, 0), (5, 1), (10, 0), (15, -1), (20, 0)])
        self.assertEqual(r.status_code, 200)

        # malformed payload -> rejected, old samples/report remain
        r2 = self.c.post(
            f"/cells/{cell}/samples",
            files={"file": ("x.csv", b"foo,bar\n1,2\n", "text/csv")},
        )
        self.assertEqual(r2.status_code, 400)
        rep = self.c.get(f"/cells/{cell}/report").json()
        self.assertEqual(rep["version"], 1)
        self.assertEqual(rep["total_cycles"], 1)

    def test_unknown_cell_404(self):
        self.assertEqual(self.c.get("/cells/NOPE/report").status_code, 404)


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        self.cell = "CELL-IDEM"
        self.rows = [(0, 0.0), (10, 1.5), (20, 0.0), (30, -2.5), (40, 0.0)]
        r = upload(self.c, self.cell, self.rows)
        self.assertEqual(r.status_code, 200, r.text)
        self.initial_version = r.json()["version"]

    def test_same_batch_is_idempotent(self):
        r1 = upload(self.c, self.cell, self.rows)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["inserted"], 0)
        self.assertEqual(r1.json()["duplicate"], 5)
        # a new report version is still committed from the same state
        self.assertEqual(r1.json()["version"], self.initial_version + 1)

        rep = self.c.get(f"/cells/{self.cell}/report").json()
        self.assertEqual(rep["total_cycles"], 1)
        self.assertAlmostEqual(rep["cycles"][0]["charge_ah"], 1.5 * 10 / 3600)

    def test_different_value_rejects_whole_batch(self):
        # one colliding point differs + brand-new points: entire batch rejected
        mixed = [(0, 0.0), (10, 9.99), (50, 1.0), (60, 0.0)]
        r = upload(self.c, self.cell, mixed)
        self.assertEqual(r.status_code, 409, r.text)

        rep = self.c.get(f"/cells/{self.cell}/report").json()
        self.assertEqual(rep["version"], 1)  # unchanged
        self.assertEqual(rep["total_cycles"], 1)
        # the brand-new point from the rejected batch must not exist
        r2 = upload(self.c, self.cell, [(50, 1.0)])
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["inserted"], 1)


class LatePointTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        self.cell = "CELL-LATE"

    def test_late_point_rewrites_and_renumbers_cycles(self):
        # first batch: one complete cycle plus an unclosed positive tail
        first = [
            (0, 0.0),
            (10, 1.0),
            (20, 0.0),
            (30, -1.0),
            (40, 0.0),   # cycle 1 closes
            (50, 1.0),   # unclosed tail
        ]
        r = upload(self.c, self.cell, first)
        self.assertEqual(r.json()["total_cycles"], 1)

        # late batch fills in an earlier zero that splits nothing, then
        # closes the tail into a second cycle
        late = [
            (5, 0.0),    # extra zero inside the opening wait state
            (60, 0.0),
            (70, -1.0),
            (80, 0.0),   # tail now forms cycle 2
        ]
        r2 = upload(self.c, self.cell, late)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["version"], r.json()["version"] + 1)
        rep = self.c.get(f"/cells/{self.cell}/report").json()
        self.assertEqual(rep["version"], 2)
        self.assertEqual(rep["total_cycles"], 2)
        self.assertEqual([c["cycle_no"] for c in rep["cycles"]], [1, 2])
        self.assertEqual(rep["cycles"][1]["started_at"], 40)
        self.assertEqual(rep["cycles"][1]["ended_at"], 80)

    def test_late_point_can_invalidate_previous_cycle(self):
        cell = "CELL-LATE2"
        first = [(0, 0.0), (10, 1.0), (20, 0.0), (30, -1.0), (40, 0.0)]
        upload(self.c, cell, first)
        # harmless extra zero first, then a sign-flip point at t=15:
        # no zero between t=10 (+1 A) and t=15 (-1 A), so the charge
        # segment is invalidated and the previously stored cycle disappears
        upload(self.c, cell, [(5, 0.0)])
        r = upload(self.c, cell, [(15, -1.0)])
        self.assertEqual(r.status_code, 200, r.text)
        rep = self.c.get(f"/cells/{cell}/report").json()
        self.assertEqual(rep["total_cycles"], 0)
        self.assertEqual(rep["cycles"], [])


class GapHttpTests(unittest.TestCase):
    def test_gap_truncates_over_http(self):
        c = TestClient(app)
        cell = "CELL-GAP"
        rows = [
            (0, 0.0),
            (10, 2.0),
            (71, 0.0),   # 61 s gap
            (80, -3.0),
            (90, 0.0),
        ]
        upload(c, cell, rows)
        rep = c.get(f"/cells/{cell}/report").json()
        self.assertEqual(rep["total_cycles"], 0)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_uploads_and_queries_are_consistent(self):
        cell = "CELL-CONC"
        errors = []
        barrier = threading.Barrier(8)

        def writer(n):
            try:
                barrier.wait()
                c = TestClient(app)
                # each writer owns a disjoint time region -> no conflicts
                base = n * 1000
                rows = [
                    (base + 0, 0.0),
                    (base + 10, 1.0),
                    (base + 20, 0.0),
                    (base + 30, -1.0),
                    (base + 40, 0.0),
                ]
                r = upload(c, cell, rows)
                if r.status_code != 200:
                    errors.append(f"writer {n}: {r.status_code} {r.text}")
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        def reader():
            try:
                barrier.wait()
                c = TestClient(app)
                for _ in range(50):
                    rr = c.get(f"/cells/{cell}/report")
                    if rr.status_code == 404:
                        continue  # first upload not committed yet
                    rep = rr.json()
                    # version / total / detail must always be one state
                    self.assertEqual(rep["total_cycles"], len(rep["cycles"]))
                    nos = [x["cycle_no"] for x in rep["cycles"]]
                    self.assertEqual(nos, list(range(1, rep["total_cycles"] + 1)))
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        rep = TestClient(app).get(f"/cells/{cell}/report").json()
        self.assertEqual(rep["total_cycles"], 4)
        self.assertEqual(len(rep["cycles"]), 4)
        # every committed version must be internally consistent
        import sqlite3

        conn = sqlite3.connect(os.environ["BATTERY_DB_PATH"])
        rows = conn.execute(
            "SELECT v.version, v.total_cycles, COUNT(c.cycle_no) FROM"
            " report_versions v LEFT JOIN cycles c"
            " ON c.cell_id=v.cell_id AND c.version=v.version"
            " WHERE v.cell_id=? GROUP BY v.version",
            (cell,),
        ).fetchall()
        conn.close()
        for version, total, detail_count in rows:
            self.assertEqual(total, detail_count, f"version {version} inconsistent")

    def test_concurrent_conflicting_upload_one_rejected_no_partial_state(self):
        cell = "CELL-CONC-CONFLICT"
        # seed a base point
        c = TestClient(app)
        self.assertEqual(upload(c, cell, [(0, 0.0), (10, 1.0)]).status_code, 200)

        outcomes = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def push(value):
            barrier.wait()
            cli = TestClient(app)
            r = upload(cli, cell, [(10, value), (20 + int(value * 100), 0.0)])
            with lock:
                outcomes.append((value, r.status_code))

        t1 = threading.Thread(target=push, args=(1.0,))   # same as stored
        t2 = threading.Thread(target=push, args=(2.0,))   # collides at ts=10
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        statuses = sorted(s for _, s in outcomes)
        self.assertEqual(statuses[0], 200)
        self.assertEqual(statuses[1], 409)

        # stored value at ts=10 must be the original 1.0 A
        import sqlite3

        conn = sqlite3.connect(os.environ["BATTERY_DB_PATH"])
        val = conn.execute(
            "SELECT current_ma FROM samples WHERE cell_id=? AND ts=10", (cell,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(val, round(1.0 * 1_000_000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
