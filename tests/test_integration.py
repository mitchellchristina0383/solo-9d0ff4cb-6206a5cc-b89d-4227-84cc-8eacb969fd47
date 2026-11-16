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


class StepTableHttpTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)

    def steps_csv(self, rows, header="step_no,start_utc_seconds,end_utc_seconds,polarity\n"):
        return header + "".join(
            f"{no},{start},{end},{pol}\n" for no, start, end, pol in rows
        )

    def upload_steps(self, cell, rows, header=None):
        text = self.steps_csv(rows) if header is None else header + "".join(
            f"{no},{start},{end},{pol}\n" for no, start, end, pol in rows
        )
        return self.c.post(
            f"/cells/{cell}/steps",
            files={"file": ("steps.csv", text, "text/csv")},
        )

    def test_replace_step_table_recomputes_diagnostics(self):
        cell = "CELL-STEPS-1"
        upload(self.c, cell, [(0, 0.0), (10, 2.0), (20, 0.0), (30, -3.0), (40, 0.0)])
        r = self.upload_steps(
            cell, [(1, 10, 20, "positive"), (2, 30, 40, "negative")]
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["step_version"], 1)
        self.assertEqual(body["step_count"], 2)
        self.assertEqual(body["report_version"], 1)

        rep = self.c.get(f"/cells/{cell}/report").json()
        self.assertEqual(rep["total_cycles"], 1)  # capacity report unaffected

        d = self.c.get(f"/cells/{cell}/diagnostics").json()
        self.assertEqual(d["diagnostics_version"], body["diagnostics_version"])
        self.assertEqual(d["max_pairs"], 2)
        self.assertFalse(d["ambiguous"])
        self.assertEqual(d["unmatched_steps"], [])
        self.assertEqual(d["unmatched_segments"], [])
        self.assertEqual(
            [p["capacity_ah"] for p in d["matched"]],
            [2.0 * 10 / 3600, 3.0 * 10 / 3600],
        )
        self.assertEqual(d["step_version"], 1)
        self.assertEqual(d["report_version"], 1)
        self.assertTrue(d["consistent"])

    def test_overlap_rejects_and_keeps_old_table(self):
        cell = "CELL-STEPS-OVERLAP"
        upload(self.c, cell, [(0, 0.0), (10, 1.0), (20, 0.0)])
        ok = self.upload_steps(cell, [(1, 10, 20, "positive")])
        self.assertEqual(ok.status_code, 200, ok.text)
        before = self.c.get(f"/cells/{cell}/steps").json()

        bad = self.upload_steps(
            cell, [(1, 10, 20, "positive"), (2, 19, 30, "negative")]
        )
        self.assertEqual(bad.status_code, 400, bad.text)
        after = self.c.get(f"/cells/{cell}/steps").json()
        self.assertEqual(after, before)  # old table untouched

        d = self.c.get(f"/cells/{cell}/diagnostics").json()
        self.assertEqual(d["step_version"], 1)
        self.assertEqual(d["max_pairs"], 1)

    def test_touching_ranges_are_allowed(self):
        cell = "CELL-STEPS-TOUCH"
        r = self.upload_steps(
            cell,
            [
                (1, 10, 20, "positive"),
                (2, 20, 30, "negative"),
                (3, 30, 40, "positive"),
            ],
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_bad_polarity_and_bad_rows_rejected(self):
        cell = "CELL-STEPS-BAD"
        self.assertEqual(
            self.upload_steps(cell, [(1, 10, 20, "sideways")]).status_code, 400
        )
        self.assertEqual(
            self.upload_steps(cell, [(1, 20, 10, "positive")]).status_code, 400
        )
        self.assertEqual(
            self.upload_steps(
                cell, [(1, 10, 20, "positive"), (1, 30, 40, "positive")]
            ).status_code,
            400,
        )
        self.assertEqual(
            self.c.get(f"/cells/{cell}/steps").status_code, 404
        )

    def test_chinese_headers_and_polarity_words(self):
        cell = "CELL-STEPS-CN"
        upload(self.c, cell, [(0, 0.0), (10, 2.0), (20, 0.0), (30, -2.0), (40, 0.0)])
        text = "工步号,起始 UTC 秒,结束 UTC 秒,电流极性\n1,10,20,充电\n2,30,40,放电\n"
        r = self.c.post(
            f"/cells/{cell}/steps",
            files={"file": ("steps.csv", text, "text/csv")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        d = self.c.get(f"/cells/{cell}/diagnostics").json()
        self.assertEqual(d["max_pairs"], 2)
        self.assertFalse(d["ambiguous"])

    def test_polarity_sign_forms(self):
        cell = "CELL-STEPS-SIGN"
        r = self.upload_steps(
            cell,
            [(1, 10, 20, "+"), (2, 30, 40, "-1")],
        )
        self.assertEqual(r.status_code, 200, r.text)
        steps = self.c.get(f"/cells/{cell}/steps").json()["steps"]
        self.assertEqual([s["polarity_sign"] for s in steps], [1, -1])

    def test_extra_columns_and_blank_lines_are_ignored(self):
        cell = "CELL-STEPS-EXTRA"
        text = (
            "note,step_no,start_utc_seconds,end_utc_seconds,polarity,comment\n"
            "x,1,10,20,positive,ok\n"
            "\n"
            "y,2,30,40,negative,ok\n"
        )
        r = self.c.post(
            f"/cells/{cell}/steps",
            files={"file": ("steps.csv", text, "text/csv")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["step_count"], 2)

    def test_step_numbers_need_not_preserve_file_order(self):
        cell = "CELL-STEPS-ORDER"
        r = self.upload_steps(
            cell,
            [(9, 50, 60, "positive"), (1, 10, 20, "positive")],
        )
        self.assertEqual(r.status_code, 200, r.text)
        steps = self.c.get(f"/cells/{cell}/steps").json()["steps"]
        self.assertEqual([s["step_no"] for s in steps], [1, 9])

    def test_late_point_rewrites_diagnostics(self):
        cell = "CELL-STEPS-LATE"
        upload(self.c, cell, [(0, 0.0), (10, 2.0), (20, 0.0)])
        self.upload_steps(cell, [(1, 10, 20, "positive")])
        d1 = self.c.get(f"/cells/{cell}/diagnostics").json()
        self.assertEqual(d1["max_pairs"], 1)
        self.assertTrue(d1["consistent"])

        # A late point at t=15 turns the run into a sign flip without a zero,
        # so the measured segment disappears and the step becomes unmatched.
        r = upload(self.c, cell, [(15, -2.0)])
        self.assertEqual(r.status_code, 200, r.text)
        rep = self.c.get(f"/cells/{cell}/report").json()
        d2 = self.c.get(f"/cells/{cell}/diagnostics").json()
        self.assertEqual(d2["report_version"], rep["version"])
        self.assertGreater(
            d2["diagnostics_version"], d1["diagnostics_version"]
        )
        self.assertEqual(d2["max_pairs"], 0)
        self.assertEqual(d2["unmatched_steps"][0]["step_no"], 1)
        # the late negative point closes at t=20 without a zero before it,
        # so the positive run is gone and a stray negative segment remains
        self.assertEqual(
            [s["polarity"] for s in d2["unmatched_segments"]], ["negative"]
        )
        self.assertTrue(d2["consistent"])

    def test_step_replacement_rewrites_diagnostics_without_new_samples(self):
        cell = "CELL-STEPS-REPL"
        upload(self.c, cell, [(0, 0.0), (10, 2.0), (20, 0.0), (30, -3.0), (40, 0.0)])
        self.upload_steps(cell, [(1, 10, 20, "positive")])
        d1 = self.c.get(f"/cells/{cell}/diagnostics").json()
        self.assertEqual(d1["max_pairs"], 1)

        self.upload_steps(
            cell, [(1, 10, 20, "positive"), (2, 30, 40, "negative")]
        )
        d2 = self.c.get(f"/cells/{cell}/diagnostics").json()
        self.assertEqual(d2["max_pairs"], 2)
        self.assertEqual(d2["step_version"], 2)
        self.assertGreater(d2["diagnostics_version"], d1["diagnostics_version"])
        self.assertEqual(d2["report_version"], d1["report_version"])

    def test_state_endpoint_versions_agree(self):
        cell = "CELL-STATE"
        upload(self.c, cell, [(0, 0.0), (10, 2.0), (20, 0.0), (30, -3.0), (40, 0.0)])
        self.upload_steps(
            cell, [(1, 10, 20, "positive"), (2, 30, 40, "negative")]
        )
        state = self.c.get(f"/cells/{cell}/state").json()
        self.assertEqual(state["report"]["version"], 1)
        self.assertEqual(state["step_table"]["step_version"], 1)
        self.assertEqual(state["diagnostics"]["report_version"], 1)
        self.assertEqual(state["diagnostics"]["step_version"], 1)
        self.assertTrue(state["diagnostics"]["consistent"])
        self.assertEqual(
            state["diagnostics"]["samples_digest"],
            state["diagnostics"]["current_samples_digest"],
        )
        self.assertEqual(len(state["report"]["cycles"]), 1)

    def test_tie_over_http_lists_candidates(self):
        cell = "CELL-STEPS-TIE"
        samples = [
            (90, 0.0), (100, 1.0), (104, 0.0),
            (105, 1.0), (109, 0.0),
        ]
        upload(self.c, cell, samples)
        self.upload_steps(cell, [(1, 100, 110, "positive")])
        d = self.c.get(f"/cells/{cell}/diagnostics").json()
        self.assertEqual(d["max_pairs"], 1)
        self.assertTrue(d["ambiguous"])
        self.assertEqual(d["matched"], [])
        self.assertEqual(len(d["candidates"]), 2)
        self.assertEqual(d["candidate_count"], d["candidate_count_exact"])
        self.assertEqual(
            d["ambiguous_steps"][0]["candidate_segment_nos"], [1, 2]
        )

    def test_upload_without_step_table_has_no_pairs(self):
        cell = "CELL-STEPS-NONE"
        upload(self.c, cell, [(0, 0.0), (10, 2.0), (20, 0.0)])
        d = self.c.get(f"/cells/{cell}/diagnostics").json()
        self.assertEqual(d["step_version"], 0)
        self.assertEqual(d["max_pairs"], 0)
        self.assertEqual(d["unmatched_steps"], [])
        self.assertEqual(len(d["unmatched_segments"]), 1)


class StepTableFailureTests(unittest.TestCase):
    def setUp(self):
        # Unexpected storage failures surface as HTTP 500; the test client
        # must not re-raise them so the rollback can be observed over HTTP.
        self.c = TestClient(app, raise_server_exceptions=False)

    def test_failed_diagnostic_write_keeps_old_step_table(self):
        from unittest import mock

        cell = "CELL-STEPS-ROLLBACK"
        self.c.post(
            f"/cells/{cell}/samples",
            files={
                "file": (
                    "s.csv",
                    "timestamp,current\n0,0\n10,1\n20,0\n",
                    "text/csv",
                )
            },
        )
        files = {
            "file": (
                "steps.csv",
                "step_no,start_utc_seconds,end_utc_seconds,polarity\n"
                "1,10,20,positive\n",
                "text/csv",
            )
        }
        self.assertEqual(
            self.c.post(f"/cells/{cell}/steps", files=files).status_code, 200
        )
        before = self.c.get(f"/cells/{cell}/steps").json()

        new_files = {
            "file": (
                "steps.csv",
                "step_no,start_utc_seconds,end_utc_seconds,polarity\n"
                "1,10,20,positive\n2,30,40,negative\n",
                "text/csv",
            )
        }
        with mock.patch(
            "app.db._write_diagnostics", side_effect=RuntimeError("boom")
        ):
            r = self.c.post(f"/cells/{cell}/steps", files=new_files)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(self.c.get(f"/cells/{cell}/steps").json(), before)
        d = self.c.get(f"/cells/{cell}/diagnostics").json()
        self.assertEqual(d["step_version"], 1)
        self.assertEqual(d["max_pairs"], 1)

    def test_failed_sample_import_keeps_step_table_and_diagnostics(self):
        from unittest import mock

        cell = "CELL-SAMPLE-ROLLBACK"
        files = {
            "file": (
                "steps.csv",
                "step_no,start_utc_seconds,end_utc_seconds,polarity\n"
                "1,10,20,positive\n",
                "text/csv",
            )
        }
        self.c.post(f"/cells/{cell}/steps", files=files)
        before_steps = self.c.get(f"/cells/{cell}/steps").json()
        before_diag = self.c.get(f"/cells/{cell}/diagnostics").json()

        with mock.patch(
            "app.db._write_diagnostics", side_effect=RuntimeError("boom")
        ):
            r = upload(self.c, cell, [(0, 0.0), (10, 1.0), (20, 0.0)])
        self.assertEqual(r.status_code, 500)
        self.assertEqual(self.c.get(f"/cells/{cell}/steps").json(), before_steps)
        self.assertEqual(
            self.c.get(f"/cells/{cell}/diagnostics").json(), before_diag
        )
        # the rejected samples must not have been stored either
        r2 = upload(self.c, cell, [(0, 0.0), (10, 1.0), (20, 0.0)])
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["inserted"], 3)


class StepConcurrencyTests(unittest.TestCase):
    def test_concurrent_step_replacements_are_serialized(self):
        cell = "CELL-STEPS-CONC"
        TestClient(app).post(
            f"/cells/{cell}/samples",
            files={
                "file": (
                    "s.csv",
                    "timestamp,current\n0,0\n10,1\n20,0\n30,-1\n40,0\n",
                    "text/csv",
                )
            },
        )
        errors = []
        barrier = threading.Barrier(5)

        def writer(n):
            try:
                barrier.wait()
                c = TestClient(app)
                rows = [(1, 10, 20, "positive"), (2, 30, 40, "negative")]
                text = "step_no,start_utc_seconds,end_utc_seconds,polarity\n"
                text += "".join(f"{a},{b},{e},{d}\n" for a, b, e, d in rows)
                r = c.post(
                    f"/cells/{cell}/steps",
                    files={"file": ("s.csv", text, "text/csv")},
                )
                if r.status_code != 200:
                    errors.append((n, r.status_code, r.text))
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        def reader():
            try:
                barrier.wait()
                c = TestClient(app)
                for _ in range(20):
                    rr = c.get(f"/cells/{cell}/state")
                    if rr.status_code == 404:
                        continue
                    state = rr.json()
                    if state["diagnostics"]["consistent"]:
                        pass
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        final = TestClient(app).get(f"/cells/{cell}/state").json()
        self.assertEqual(final["step_table"]["step_version"], 3)
        self.assertEqual(final["step_table"]["step_count"], 2)
        self.assertEqual(final["diagnostics"]["step_version"], 3)
        self.assertTrue(final["diagnostics"]["consistent"])
        self.assertEqual(final["diagnostics"]["max_pairs"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
