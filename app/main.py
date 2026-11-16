"""FastAPI application: battery cell sample upload and cycle reports."""

from __future__ import annotations

import os

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

from .csv_import import CSVParseError, parse_samples_csv
from .db import ConflictError, Database, ImportError_
from .steps_csv import StepTableError, parse_steps_csv

DB_PATH = os.environ.get("BATTERY_DB_PATH", "/data/battery.db")

app = FastAPI(
    title="Battery Cell Cycle Capacity API",
    version="2.0.0",
    description=(
        "Upload UTC-second current samples per cell and an approved "
        "charge/discharge step table. Zero current marks segment boundaries; "
        "only zero→positive→zero→negative→zero sequences count as cycles. "
        "Gaps over 60 s truncate a sequence. Ah is the left-value current "
        "integral over each interval. The diagnostics endpoint pairs "
        "zero-closed measured segments with same-polarity steps inside a 15 s "
        "start/end tolerance, maximising pair count first and minimising "
        "total deviation second; ties are reported as explicit candidates."
    ),
)
db = Database(DB_PATH)


class CycleOut(BaseModel):
    cycle_no: int
    charge_ah: float
    discharge_ah: float
    started_at: int
    ended_at: int


class ReportOut(BaseModel):
    cell_id: str
    version: int
    total_cycles: int
    created_at: str | None
    cycles: list[CycleOut]


class UploadOut(BaseModel):
    cell_id: str
    inserted: int
    duplicate: int
    version: int
    total_cycles: int
    diagnostics_version: int


class StepTableUploadOut(BaseModel):
    cell_id: str
    step_version: int
    step_count: int
    report_version: int
    diagnostics_version: int


class StepOut(BaseModel):
    step_no: int
    start_ts: int
    end_ts: int
    polarity: str
    polarity_sign: int


class StepTableOut(BaseModel):
    cell_id: str
    step_version: int
    step_count: int
    created_at: str | None
    steps: list[StepOut]


class PairOut(BaseModel):
    step_no: int
    segment_no: int
    polarity: str
    polarity_sign: int
    step_start: int
    step_end: int
    measured_start: int
    measured_end: int
    capacity_ah: float
    start_deviation_s: int
    end_deviation_s: int


class SegmentOut(BaseModel):
    segment_no: int
    polarity: str
    polarity_sign: int
    started_at: int
    ended_at: int
    capacity_ah: float


class AmbiguousStepOut(StepOut):
    candidate_segment_nos: list[int]
    may_be_unmatched: bool


class AmbiguousSegmentOut(SegmentOut):
    candidate_step_nos: list[int]
    may_be_unmatched: bool


class CandidateOut(BaseModel):
    pairs: list[PairOut]
    unmatched_step_nos: list[int]
    unmatched_segment_nos: list[int]


class DiagnosticsOut(BaseModel):
    cell_id: str
    diagnostics_version: int
    report_version: int
    step_version: int
    samples_digest: str | None
    current_samples_digest: str | None
    consistent: bool
    created_at: str | None
    max_pairs: int
    min_total_deviation: int
    ambiguous: bool
    candidate_count: int
    candidate_count_exact: int
    candidates_truncated: bool
    matched: list[PairOut]
    unmatched_steps: list[StepOut]
    unmatched_segments: list[SegmentOut]
    ambiguous_steps: list[AmbiguousStepOut]
    ambiguous_segments: list[AmbiguousSegmentOut]
    candidates: list[CandidateOut]
    segments: list[SegmentOut]
    step_table: StepTableOut | None


class StateOut(BaseModel):
    cell_id: str
    report: ReportOut
    step_table: StepTableOut | None
    diagnostics: DiagnosticsOut


@app.post(
    "/cells/{cell_id}/samples",
    response_model=UploadOut,
    tags=["upload"],
    summary="Upload a CSV batch of (UTC seconds, current A) samples",
)
async def upload_samples(cell_id: str, file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = parse_samples_csv(content)
        result = db.import_batch(cell_id, rows)
    except CSVParseError as exc:
        raise HTTPException(status_code=400, detail=f"CSV error: {exc}") from exc
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"batch rejected, no data changed: same timestamp {exc.ts} "
                f"already stored with {exc.existing} A, incoming {exc.incoming} A"
            ),
        ) from exc
    return result


@app.post(
    "/cells/{cell_id}/steps",
    response_model=StepTableUploadOut,
    tags=["steps"],
    summary="Replace the whole approved step table for a cell from a CSV",
)
async def upload_step_table(cell_id: str, file: UploadFile = File(...)):
    content = await file.read()
    try:
        steps = parse_steps_csv(content)
        result = db.replace_step_table(cell_id, steps)
    except StepTableError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"step table rejected, old table kept: {exc}",
        ) from exc
    except ImportError_ as exc:
        raise HTTPException(
            status_code=400,
            detail=f"step table rejected, old table kept: {exc}",
        ) from exc
    return result


@app.get(
    "/cells/{cell_id}/steps",
    response_model=StepTableOut,
    tags=["steps"],
    summary="Latest committed approved step table for one cell",
)
def get_step_table(cell_id: str):
    table = db.get_step_table(cell_id)
    if table is None:
        raise HTTPException(status_code=404, detail=f"unknown cell {cell_id}")
    return table


@app.get("/cells", tags=["query"], summary="List all cells with latest report info")
def list_cells():
    return db.list_cells()


@app.get(
    "/cells/{cell_id}/report",
    response_model=ReportOut,
    tags=["query"],
    summary="Latest committed cycle report for one cell",
)
def get_report(cell_id: str):
    report = db.get_report(cell_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"unknown cell {cell_id}")
    return report


@app.get(
    "/cells/{cell_id}/diagnostics",
    response_model=DiagnosticsOut,
    tags=["query"],
    summary="Latest diagnosis: unmatched steps, definite pairs, capacities",
    description=(
        "Measured segments are zero-opened and zero-closed, gap-free runs. "
        "They are paired one-to-one in time order with same-polarity steps; "
        "start and end must each be within 15 s. The optimum maximises the "
        "number of pairs, then minimises the summed start/end deviation. "
        "When the optimum is not unique, `matched` contains only pairs common "
        "to every optimum, `ambiguous_steps`/`ambiguous_segments` list the "
        "competing candidates, and `candidates` lists the enumerated optima."
    ),
)
def get_diagnostics(cell_id: str):
    diagnostics = db.get_diagnostics(cell_id)
    if diagnostics is None:
        raise HTTPException(status_code=404, detail=f"unknown cell {cell_id}")
    return diagnostics


@app.get(
    "/cells/{cell_id}/state",
    response_model=StateOut,
    tags=["query"],
    summary="Report, step table and diagnosis from one committed snapshot",
    description=(
        "Returns the report version, step-table version, diagnosis version "
        "and the sample digest from the same committed state. "
        "`diagnostics.consistent` is false if the stored diagnosis does not "
        "match the samples/version stamps read in the same snapshot."
    ),
)
def get_state(cell_id: str):
    state = db.get_state(cell_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown cell {cell_id}")
    return state


@app.get("/health", tags=["ops"], summary="Liveness probe")
def health():
    return {"status": "ok"}
