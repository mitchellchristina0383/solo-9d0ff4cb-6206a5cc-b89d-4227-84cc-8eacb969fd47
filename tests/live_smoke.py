"""Live-server smoke test.

Runs against an already-started uvicorn (default http://127.0.0.1:8000).
Covers: late-point rewrite/renumbering, gap truncation, idempotency and
conflict rejection, concurrent uploads/queries with snapshot consistency,
step-table whole-table replacement, unmatched steps, tied optima candidates
and the combined state endpoint.
"""

import concurrent.futures
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def upload(client, cell, rows, header="timestamp,current"):
    text = header + "\n" + "".join(f"{ts},{cur}\n" for ts, cur in rows)
    return client.post(
        f"{BASE}/cells/{cell}/samples",
        files={"file": ("batch.csv", text, "text/csv")},
    )


def upload_steps(client, cell, rows):
    text = "step_no,start_utc_seconds,end_utc_seconds,polarity\n" + "".join(
        f"{no},{start},{end},{pol}\n" for no, start, end, pol in rows
    )
    return client.post(
        f"{BASE}/cells/{cell}/steps",
        files={"file": ("steps.csv", text, "text/csv")},
    )


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS: {msg}")


def main():
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{BASE}/health")
        check(r.status_code == 200, "health endpoint")

        # --- 1. late point rewrites/renumbers cycles --------------------
        cell = "LIVE-LATE"
        r = upload(c, cell, [(0, 0), (10, 1), (20, 0), (30, -1), (40, 0), (50, 1)])
        check(r.status_code == 200, "first batch accepted")
        check(r.json()["total_cycles"] == 1, "one closed cycle, tail ignored")
        v1 = r.json()["version"]

        r = upload(c, cell, [(60, 0), (70, -1), (80, 0)])
        check(r.status_code == 200, "late batch accepted")
        check(r.json()["version"] == v1 + 1, "report version bumped")
        rep = c.get(f"{BASE}/cells/{cell}/report").json()
        check(rep["total_cycles"] == 2, "late point closed second cycle")
        check([x["cycle_no"] for x in rep["cycles"]] == [1, 2], "cycles renumbered 1..N")
        check(
            abs(rep["cycles"][0]["charge_ah"] - 1 * 10 / 3600) < 1e-12,
            "charge Ah from left-value integral",
        )

        # --- 2. gap > 60 s truncates ------------------------------------
        cell = "LIVE-GAP"
        r = upload(c, cell, [(0, 0), (10, 2), (71, 0), (80, -3), (90, 0)])
        rep = c.get(f"{BASE}/cells/{cell}/report").json()
        check(rep["total_cycles"] == 0, "61 s gap truncates the sequence")

        r = upload(c, cell, [(0, 0), (60, 2), (120, 0), (130, -1), (140, 0)])
        rep = c.get(f"{BASE}/cells/{cell}/report").json()
        check(rep["total_cycles"] == 1, "exactly-60 s intervals are kept")

        # --- 3. idempotency + conflict whole-batch rejection ------------
        cell = "LIVE-IDEM"
        rows = [(0, 0), (10, 1.5), (20, 0), (30, -2.5), (40, 0)]
        upload(c, cell, rows)
        r = upload(c, cell, rows)
        check(r.json()["inserted"] == 0 and r.json()["duplicate"] == 5,
              "identical re-upload is idempotent")
        before = c.get(f"{BASE}/cells/{cell}/report").json()
        r = upload(c, cell, [(10, 9.9), (99, 1)])
        check(r.status_code == 409, "different value at same ts -> 409")
        after = c.get(f"{BASE}/cells/{cell}/report").json()
        check(after["version"] == before["version"], "rejected batch keeps old version")
        check(after["total_cycles"] == before["total_cycles"], "old report unchanged")

        # --- 4. concurrency: 8 writers, parallel readers ----------------
        cell = "LIVE-CONC"

        def writer(n):
            base = n * 1000
            with httpx.Client(timeout=30) as wc:
                return upload(
                    wc, cell,
                    [(base, 0), (base + 10, 1), (base + 20, 0),
                     (base + 30, -1), (base + 40, 0)],
                ).status_code

        def reader(_):
            bad = []
            with httpx.Client(timeout=30) as rc:
                for _ in range(40):
                    rr = rc.get(f"{BASE}/cells/{cell}/report")
                    if rr.status_code == 404:
                        continue
                    rep = rr.json()
                    nos = [x["cycle_no"] for x in rep["cycles"]]
                    if rep["total_cycles"] != len(rep["cycles"]) or nos != list(
                        range(1, rep["total_cycles"] + 1)
                    ):
                        bad.append(rep)
            return bad

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            f_w = [pool.submit(writer, n) for n in range(8)]
            f_r = [pool.submit(reader, n) for n in range(8)]
            codes = [f.result() for f in f_w]
            inconsistencies = [x for f in f_r for x in f.result()]
        check(codes == [200] * 8, f"all 8 concurrent writers committed (got {codes})")
        check(not inconsistencies, "every query saw one internally consistent state")
        final = c.get(f"{BASE}/cells/{cell}/report").json()
        check(final["total_cycles"] == 8, "final report contains all 8 cycles")

        cells = c.get(f"{BASE}/cells").json()
        check(any(x["cell_id"] == cell for x in cells), "cell list endpoint")

        # --- 5. step table + diagnostics --------------------------------
        cell = "LIVE-STEPS"
        upload(c, cell, [(0, 0), (10, 2), (20, 0), (50, 1), (60, 0)])
        # planned +,+,+ but the middle step was never measured
        r = upload_steps(
            c,
            cell,
            [(1, 10, 20, "positive"), (2, 30, 40, "positive"),
             (3, 50, 60, "positive")],
        )
        check(r.status_code == 200, "step table replaced as a whole")
        d = c.get(f"{BASE}/cells/{cell}/diagnostics").json()
        check(
            [(p["step_no"], p["segment_no"]) for p in d["matched"]]
            == [(1, 1), (3, 2)],
            "missed step skipped, later steps realign",
        )
        check(
            [s["step_no"] for s in d["unmatched_steps"]] == [2],
            "missed step reported as unmatched",
        )
        check(d["ambiguous"] is False, "unique optimum is not ambiguous")
        check(
            abs(d["matched"][0]["capacity_ah"] - 2 * 10 / 3600) < 1e-12,
            "paired measured segment carries integrated capacity",
        )
        check(d["consistent"] is True, "diagnosis digest matches samples")

        # overlapping table rejected, old table kept
        r = upload_steps(
            c,
            cell,
            [(1, 10, 20, "positive"), (2, 19, 30, "negative")],
        )
        check(r.status_code == 400, "overlapping step table rejected")
        d2 = c.get(f"{BASE}/cells/{cell}/diagnostics").json()
        check(d2["step_version"] == 1, "old step table still in force")

        # --- 6. tied optimum -> candidates, no arbitrary choice ---------
        cell = "LIVE-TIE"
        upload(c, cell, [(90, 0), (100, 1), (104, 0), (105, 1), (109, 0)])
        upload_steps(c, cell, [(1, 100, 110, "positive")])
        d = c.get(f"{BASE}/cells/{cell}/diagnostics").json()
        check(d["max_pairs"] == 1, "tie: one pair is optimal")
        check(d["ambiguous"] is True, "tie flagged as ambiguous")
        check(d["matched"] == [], "tied step not reported as definite")
        check(d["candidate_count_exact"] == 2, "both tied optima counted")
        check(len(d["candidates"]) == 2, "both tied optima returned")
        check(
            d["ambiguous_steps"][0]["candidate_segment_nos"] == [1, 2],
            "competing measured segments listed",
        )

        # --- 7. state endpoint versions agree ---------------------------
        state = c.get(f"{BASE}/cells/{cell}/state").json()
        check(
            state["diagnostics"]["step_version"]
            == state["step_table"]["step_version"],
            "state: step table matches diagnosis",
        )
        check(
            state["report"]["version"] == state["diagnostics"]["report_version"],
            "state: report version matches diagnosis",
        )
        check(state["diagnostics"]["consistent"], "state: snapshot consistent")

        # --- 8. late point rewrites diagnosis ---------------------------
        cell = "LIVE-LATE-DIAG"
        upload(c, cell, [(0, 0), (10, 2), (20, 0)])
        upload_steps(c, cell, [(1, 10, 20, "positive")])
        before = c.get(f"{BASE}/cells/{cell}/diagnostics").json()
        check(before["max_pairs"] == 1, "initial diagnosis has one pair")
        upload(c, cell, [(15, -2)])
        after = c.get(f"{BASE}/cells/{cell}/diagnostics").json()
        check(after["max_pairs"] == 0, "late point invalidates the pairing")
        check(
            [s["step_no"] for s in after["unmatched_steps"]] == [1],
            "step now reported unmatched",
        )
        check(
            after["diagnostics_version"] > before["diagnostics_version"],
            "diagnosis version advanced",
        )

    print("\nALL LIVE CHECKS PASSED")


if __name__ == "__main__":
    for attempt in range(20):
        try:
            httpx.get(f"{BASE}/health", timeout=2)
            break
        except httpx.HTTPError:
            time.sleep(0.5)
    main()
