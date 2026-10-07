"""CSV revision preview and confirm for field-delivered observation corrections.

The surveyor uploads a CSV keyed by the stable observation id (``line_code``).
Every row is checked against the current draft: the record must exist, the
declared ``lock_version`` must match the current optimistic-lock version, and
the new height difference / length must be well formed. Preview is advisory;
confirm re-validates against the live draft and applies through the existing
``apply_optimistic_update`` audit/optimistic-lock path, so an original
observation is never silently overwritten and snapshots/jobs are never touched.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.schema import Observation
from app.services.snapshots import apply_optimistic_update

REQUIRED_COLUMNS = ("line_code", "lock_version", "observed_delta_m", "distance_m")
MAX_REVISION_ROWS = 200_000


class RowStatus(StrEnum):
    APPLICABLE = "applicable"
    VERSION_CONFLICT = "version_conflict"
    MISSING = "missing_record"
    DUPLICATE = "duplicate_line"
    INVALID = "invalid_row"


class ApplyStrategy(StrEnum):
    ALL_OR_NOTHING = "all_or_nothing"
    PER_ROW = "per_row"


class Lockable(Protocol):
    """Minimal record the classifier needs; satisfied by Observation."""

    id: int
    line_code: str
    lock_version: int
    observed_delta_m: Any
    distance_m: Any


@dataclass
class ParsedRow:
    row: int
    line_code: str
    lock_version: int
    observed_delta_m: float
    distance_m: float


@dataclass
class InvalidRow:
    row: int
    line_code: str | None
    error: str


@dataclass
class Assessment:
    row: int
    line_code: str | None
    status: RowStatus
    parsed: ParsedRow | None = None
    observation: Lockable | None = None
    detail: str | None = None


def parse_revision_csv(text: str) -> list[ParsedRow | InvalidRow]:
    """Parse the revision CSV. Structural problems raise 400; per-row problems
    become InvalidRow entries so the surveyor sees every issue in one report."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))  # tolerate Excel UTF-8 BOM
    records = [record for record in reader if any(cell.strip() for cell in record)]
    if not records:
        raise HTTPException(400, "revision file is empty")
    header = [cell.strip() for cell in records[0]]
    missing = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing:
        raise HTTPException(400, f"revision csv missing required columns: {missing}")
    index = {column: header.index(column) for column in REQUIRED_COLUMNS}

    data_records = records[1:]
    if len(data_records) > MAX_REVISION_ROWS:
        raise HTTPException(400, f"revision file exceeds {MAX_REVISION_ROWS} rows")

    parsed: list[ParsedRow | InvalidRow] = []
    for row_number, record in enumerate(data_records, start=1):

        def cell(column: str) -> str:
            position = index[column]
            return record[position].strip() if position < len(record) else ""

        line_code = cell("line_code")
        if not line_code:
            parsed.append(InvalidRow(row_number, None, "missing line_code"))
            continue
        try:
            lock_version = int(cell("lock_version"))
            if lock_version < 1:
                raise ValueError
        except ValueError:
            parsed.append(InvalidRow(row_number, line_code, f"invalid lock_version: {cell('lock_version')!r}"))
            continue
        try:
            observed_delta_m = float(cell("observed_delta_m"))
            if not math.isfinite(observed_delta_m):
                raise ValueError
        except ValueError:
            parsed.append(
                InvalidRow(row_number, line_code, f"invalid observed_delta_m: {cell('observed_delta_m')!r}")
            )
            continue
        try:
            distance_m = float(cell("distance_m"))
            if not math.isfinite(distance_m) or distance_m <= 0:
                raise ValueError
        except ValueError:
            parsed.append(InvalidRow(row_number, line_code, f"invalid distance_m: {cell('distance_m')!r}"))
            continue
        parsed.append(ParsedRow(row_number, line_code, lock_version, observed_delta_m, distance_m))
    return parsed


def assess_rows(
    parsed: list[ParsedRow | InvalidRow],
    observations_by_line: dict[str, Lockable],
) -> list[Assessment]:
    """Classify every row against the current draft state."""
    assessments: list[Assessment] = []
    seen: set[str] = set()
    for item in parsed:
        if isinstance(item, InvalidRow):
            assessments.append(Assessment(item.row, item.line_code, RowStatus.INVALID, detail=item.error))
            continue
        if item.line_code in seen:
            assessments.append(
                Assessment(
                    item.row,
                    item.line_code,
                    RowStatus.DUPLICATE,
                    parsed=item,
                    detail="duplicate line_code in file; first occurrence wins",
                )
            )
            continue
        seen.add(item.line_code)
        observation = observations_by_line.get(item.line_code)
        if observation is None:
            assessments.append(
                Assessment(
                    item.row,
                    item.line_code,
                    RowStatus.MISSING,
                    parsed=item,
                    detail="no observation with this line_code in project",
                )
            )
            continue
        current_version = int(observation.lock_version)
        if current_version != item.lock_version:
            assessments.append(
                Assessment(
                    item.row,
                    item.line_code,
                    RowStatus.VERSION_CONFLICT,
                    parsed=item,
                    observation=observation,
                    detail=f"file expects lock_version {item.lock_version}, current is {current_version}",
                )
            )
            continue
        assessments.append(
            Assessment(item.row, item.line_code, RowStatus.APPLICABLE, parsed=item, observation=observation)
        )
    return assessments


def assess_csv(csv_text: str, observations_by_line: dict[str, Lockable]) -> list[Assessment]:
    return assess_rows(parse_revision_csv(csv_text), observations_by_line)


def build_preview(project_id: int, assessments: list[Assessment]) -> dict[str, Any]:
    counts = {status.value: 0 for status in RowStatus}
    rows: list[dict[str, Any]] = []
    for assessment in assessments:
        counts[assessment.status.value] += 1
        row: dict[str, Any] = {
            "row": assessment.row,
            "line_code": assessment.line_code,
            "status": assessment.status.value,
        }
        if assessment.observation is not None:
            observation = assessment.observation
            row["observation_id"] = observation.id
            row["current"] = {
                "lock_version": int(observation.lock_version),
                "observed_delta_m": float(observation.observed_delta_m),
                "distance_m": float(observation.distance_m),
            }
        if assessment.parsed is not None:
            row["requested"] = {
                "lock_version": assessment.parsed.lock_version,
                "observed_delta_m": assessment.parsed.observed_delta_m,
                "distance_m": assessment.parsed.distance_m,
            }
        if assessment.detail:
            row["detail"] = assessment.detail
        rows.append(row)
    return {
        "project_id": project_id,
        "total_rows": len(assessments),
        "counts": counts,
        "rows": rows,
    }


def apply_assessments(
    db: Session,
    project_id: int,
    assessments: list[Assessment],
    *,
    strategy: ApplyStrategy,
    actor: str = "surveyor",
) -> dict[str, Any]:
    """Apply applicable rows through the shared optimistic-lock/audit path.

    ``all_or_nothing``: any non-applicable row rejects the whole file untouched.
    ``per_row``: applicable rows are applied, the rest are reported as skipped.
    """
    not_applicable = [a for a in assessments if a.status != RowStatus.APPLICABLE]
    if strategy == ApplyStrategy.ALL_OR_NOTHING and not_applicable:
        receipts = []
        for assessment in assessments:
            if assessment.status == RowStatus.APPLICABLE:
                receipts.append(
                    _receipt(
                        assessment,
                        "blocked_by_strategy",
                        detail="file rejected: all_or_nothing requires every row to be applicable",
                    )
                )
            else:
                receipts.append(_receipt(assessment, assessment.status.value))
        return {
            "project_id": project_id,
            "strategy": strategy.value,
            "applied_count": 0,
            "skipped_count": len(assessments),
            "draft_changed": False,
            "rows": receipts,
        }

    receipts = []
    applied = 0
    for assessment in assessments:
        if assessment.status != RowStatus.APPLICABLE:
            receipts.append(_receipt(assessment, assessment.status.value))
            continue
        observation = assessment.observation
        parsed = assessment.parsed
        assert observation is not None and parsed is not None
        version_before = int(observation.lock_version)
        apply_optimistic_update(
            db,
            observation,
            {"observed_delta_m": parsed.observed_delta_m, "distance_m": parsed.distance_m},
            expected_version=parsed.lock_version,
            actor=actor,
        )
        applied += 1
        receipts.append(
            _receipt(
                assessment,
                "applied",
                lock_version_before=version_before,
                lock_version_after=int(observation.lock_version),
            )
        )
    return {
        "project_id": project_id,
        "strategy": strategy.value,
        "applied_count": applied,
        "skipped_count": len(assessments) - applied,
        "draft_changed": applied > 0,
        "rows": receipts,
    }


def _receipt(assessment: Assessment, status: str, **extra: Any) -> dict[str, Any]:
    receipt: dict[str, Any] = {"row": assessment.row, "line_code": assessment.line_code, "status": status}
    if assessment.observation is not None:
        receipt["observation_id"] = assessment.observation.id
    detail = assessment.detail
    if status == RowStatus.VERSION_CONFLICT and assessment.observation is not None:
        receipt["current_lock_version"] = int(assessment.observation.lock_version)
    if detail:
        receipt["detail"] = detail
    receipt.update(extra)
    return receipt


def _observations_by_line(db: Session, project_id: int, *, lock_rows: bool = False) -> dict[str, Observation]:
    statement = select(Observation).where(Observation.project_id == project_id).order_by(Observation.id)
    if lock_rows:
        # Confirm holds the draft rows until commit so a concurrent editor cannot
        # slip a version bump between classification and the audited update.
        statement = statement.with_for_update()
    return {observation.line_code: observation for observation in db.scalars(statement).all()}


def preview_revisions(db: Session, project_id: int, csv_text: str) -> dict[str, Any]:
    return build_preview(project_id, assess_csv(csv_text, _observations_by_line(db, project_id)))


def apply_revisions(
    db: Session,
    project_id: int,
    csv_text: str,
    *,
    strategy: ApplyStrategy,
    actor: str = "surveyor",
) -> dict[str, Any]:
    assessments = assess_csv(csv_text, _observations_by_line(db, project_id, lock_rows=True))
    return apply_assessments(db, project_id, assessments, strategy=strategy, actor=actor)
