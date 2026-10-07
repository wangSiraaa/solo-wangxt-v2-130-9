"""CSV observation revision preview and confirmed application.

Field sheets arrive from external survey teams. Surveyors must preview them before
application: every row is checked against the draft observation's stable id
(line_code), the expected optimistic-lock version, the height difference and the
distance. The draft observation is only ever changed through the existing
optimistic-lock/audit path, so immutable snapshots and old jobs stay untouched.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.schema import Observation
from app.services.snapshots import StaleDraftError, apply_optimistic_update_with_audit

# Row states shared by preview and confirmation receipts.
APPLICABLE = "applicable"
ALREADY_APPLIED = "already_applied"
VERSION_CONFLICT = "version_conflict"
MISSING_RECORD = "missing_record"
DUPLICATE_ROW = "duplicate_row"
INVALID_ROW = "invalid_row"

BLOCKING_STATES = frozenset({VERSION_CONFLICT, MISSING_RECORD, DUPLICATE_ROW, INVALID_ROW})

# Tolerance for comparing a CSV number to the stored value. Stored distances have
# 3 fractional digits and deltas 9; 1e-9 m safely absorbs CSV rounding noise.
VALUE_EPSILON = Decimal("0.000000001")

_HEADER_ALIASES = {
    "line_code": "line_code",
    "linecode": "line_code",
    "line": "line_code",
    "stable_id": "line_code",
    "observation_id": "line_code",
    "测段编号": "line_code",
    "测段id": "line_code",
    "测段": "line_code",
    "线号": "line_code",
    "lock_version": "lock_version",
    "version": "lock_version",
    "原锁版本": "lock_version",
    "锁版本": "lock_version",
    "版本": "lock_version",
    "observed_delta_m": "observed_delta_m",
    "delta_m": "observed_delta_m",
    "height_diff_m": "observed_delta_m",
    "高差": "observed_delta_m",
    "高差m": "observed_delta_m",
    "观测高差": "observed_delta_m",
    "distance_m": "distance_m",
    "distance": "distance_m",
    "length_m": "distance_m",
    "长度": "distance_m",
    "长度m": "distance_m",
    "测段长度": "distance_m",
}

REQUIRED_COLUMNS = ("line_code", "lock_version", "observed_delta_m", "distance_m")
COLUMN_LABELS = {
    "line_code": "测段稳定 ID (line_code)",
    "lock_version": "原锁版本 (lock_version)",
    "observed_delta_m": "高差 (observed_delta_m)",
    "distance_m": "长度 (distance_m)",
}


class RevisionStrategy(StrEnum):
    ALL_OR_NOTHING = "all_or_nothing"
    PER_ROW = "per_row"


@dataclass
class _RawRow:
    row_number: int
    line_code: str
    lock_version_text: str
    delta_text: str
    distance_text: str


@dataclass
class RevisionRowResult:
    row_number: int
    line_code: str | None
    state: str = ""
    expected_lock_version: int | None = None
    current_lock_version: int | None = None
    current_observed_delta_m: float | None = None
    current_distance_m: float | None = None
    proposed_observed_delta_m: float | None = None
    proposed_distance_m: float | None = None
    new_lock_version: int | None = None
    audit_event_id: int | None = None
    detail: str | None = None
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "line_code": self.line_code,
            "state": self.state,
            "detail": self.detail,
            "issues": self.issues,
            "expected_lock_version": self.expected_lock_version,
            "current_lock_version": self.current_lock_version,
            "new_lock_version": self.new_lock_version,
            "current_observed_delta_m": self.current_observed_delta_m,
            "current_distance_m": self.current_distance_m,
            "proposed_observed_delta_m": self.proposed_observed_delta_m,
            "proposed_distance_m": self.proposed_distance_m,
            "audit_event_id": self.audit_event_id,
        }


class RevisionSheetError(ValueError):
    """The CSV sheet itself cannot be read (bad encoding/headers/empty)."""


def decode_sheet(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def _parse_sheet(content: bytes) -> list[_RawRow]:
    text = decode_sheet(content)
    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration:
        raise RevisionSheetError("CSV 文件为空：缺少表头") from None

    header = [_HEADER_ALIASES.get(cell.strip().lower(), cell.strip().lower()) for cell in header_row]
    missing = [COLUMN_LABELS[column] for column in REQUIRED_COLUMNS if column not in header]
    if missing:
        raise RevisionSheetError(f"CSV 缺少必需列：{', '.join(missing)}")
    index = {column: header.index(column) for column in REQUIRED_COLUMNS}

    raw_rows: list[_RawRow] = []
    for offset, cells in enumerate(reader, start=2):
        if not any(cell.strip() for cell in cells):
            continue  # trailing blank line, not a revision attempt
        raw_rows.append(
            _RawRow(
                row_number=offset,
                line_code=cells[index["line_code"]].strip() if index["line_code"] < len(cells) else "",
                lock_version_text=cells[index["lock_version"]].strip()
                if index["lock_version"] < len(cells)
                else "",
                delta_text=cells[index["observed_delta_m"]].strip()
                if index["observed_delta_m"] < len(cells)
                else "",
                distance_text=cells[index["distance_m"]].strip()
                if index["distance_m"] < len(cells)
                else "",
            )
        )
    if not raw_rows:
        raise RevisionSheetError("CSV 文件没有任何修订行")
    return raw_rows


def _to_decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


def _close_enough(current: Decimal, proposed: Decimal) -> bool:
    return abs(current - proposed) <= VALUE_EPSILON


def preview_observation_revisions(db: Session, project_id: int, content: bytes) -> dict[str, Any]:
    """Validate every CSV row without modifying anything.

    Read-only: no lock is taken and no value or version is changed.
    """
    raw_rows = _parse_sheet(content)

    observations = {
        obs.line_code: obs
        for obs in db.scalars(select(Observation).where(Observation.project_id == project_id)).all()
    }

    seen: set[str] = set()
    results: list[RevisionRowResult] = []
    counts = {
        APPLICABLE: 0,
        ALREADY_APPLIED: 0,
        VERSION_CONFLICT: 0,
        MISSING_RECORD: 0,
        DUPLICATE_ROW: 0,
        INVALID_ROW: 0,
    }

    for raw in raw_rows:
        result = RevisionRowResult(row_number=raw.row_number, line_code=raw.line_code or None)
        issues: list[str] = []

        if not raw.line_code:
            issues.append("缺少测段稳定 ID")
        lock_version: int | None = None
        if not raw.lock_version_text:
            issues.append("缺少原锁版本")
        else:
            try:
                lock_version = int(raw.lock_version_text)
                if str(lock_version) != raw.lock_version_text.lstrip("+") or lock_version < 1:
                    issues.append("原锁版本必须为正整数")
                    lock_version = None
            except ValueError:
                issues.append(f"原锁版本不是整数：{raw.lock_version_text!r}")

        delta = _to_decimal(raw.delta_text) if raw.delta_text else None
        if delta is None:
            issues.append(f"高差不是有限数值：{raw.delta_text!r}")
        distance = _to_decimal(raw.distance_text) if raw.distance_text else None
        if distance is None:
            issues.append(f"长度不是有限数值：{raw.distance_text!r}")
        elif distance <= 0:
            issues.append("长度必须大于 0")
            distance = None

        duplicated = bool(raw.line_code) and raw.line_code in seen
        if raw.line_code:
            seen.add(raw.line_code)

        result.expected_lock_version = lock_version
        result.proposed_observed_delta_m = None if delta is None else float(delta)
        result.proposed_distance_m = None if distance is None else float(distance)

        obs = observations.get(raw.line_code) if raw.line_code else None
        if obs is not None:
            result.current_lock_version = int(obs.lock_version)
            result.current_observed_delta_m = float(obs.observed_delta_m)
            result.current_distance_m = float(obs.distance_m)

        if issues:
            result.state = INVALID_ROW
            result.issues = issues
        elif duplicated:
            # Duplicates are ambiguous (two expected versions, two values) and are
            # never applied silently, including on confirm.
            result.state = DUPLICATE_ROW
            result.detail = f"line_code {raw.line_code!r} 在文件中重复出现"
        elif obs is None:
            result.state = MISSING_RECORD
            result.detail = f"项目 {project_id} 中找不到测段 {raw.line_code!r}"
        else:
            current_version = int(obs.lock_version)
            same_delta = _close_enough(Decimal(obs.observed_delta_m), delta)
            same_distance = _close_enough(Decimal(obs.distance_m), distance)
            if same_delta and same_distance:
                # Re-confirming an already applied file changes nothing.
                result.state = ALREADY_APPLIED
                result.new_lock_version = None
                result.detail = "修订值与当前草稿一致，无需修改"
            elif current_version != lock_version:
                result.state = VERSION_CONFLICT
                result.detail = (
                    f"锁版本冲突：文件期望 v{lock_version}，当前草稿为 v{current_version}"
                )
            else:
                result.state = APPLICABLE
                result.new_lock_version = current_version + 1
                changes: list[str] = []
                if not same_delta:
                    changes.append("高差")
                if not same_distance:
                    changes.append("长度")
                result.detail = f"可应用：将更新{'、'.join(changes)}，锁版本 v{current_version} -> v{current_version + 1}"
                result.issues = changes

        counts[result.state] += 1
        results.append(result)

    blocking = sum(counts[state] for state in BLOCKING_STATES)
    return {
        "project_id": project_id,
        "total_rows": len(results),
        "counts": counts,
        "applicable_rows": counts[APPLICABLE],
        "blocking_rows": blocking,
        "ready_to_apply": blocking == 0,
        "rows": [r.as_dict() for r in results],
    }


def confirm_observation_revisions(
    db: Session,
    project_id: int,
    content: bytes,
    *,
    strategy: RevisionStrategy = RevisionStrategy.ALL_OR_NOTHING,
    actor: str = "surveyor",
) -> dict[str, Any]:
    """Apply a revision CSV through the existing optimistic-lock/audit semantics.

    all_or_nothing: any blocking row rolls the whole batch back (single
    transaction, no partial application).
    per_row: applicable rows are applied in independent savepoints; conflicts,
    missing, duplicate and invalid rows are reported but do not fail the batch.

    Re-confirming an already-applied sheet is idempotent: matching rows report
    already_applied, the stored value does not change and no audit chain event
    is produced.
    """
    # Re-run the full read-only classification against current draft state.
    preview = preview_observation_revisions(db, project_id, content)
    rows = {item["line_code"]: item for item in preview["rows"] if item["line_code"] is not None}
    receipts: list[RevisionRowResult] = [
        RevisionRowResult(
            row_number=item["row_number"],
            line_code=item["line_code"],
            state=item["state"],
            expected_lock_version=item["expected_lock_version"],
            current_lock_version=item["current_lock_version"],
            current_observed_delta_m=item["current_observed_delta_m"],
            current_distance_m=item["current_distance_m"],
            proposed_observed_delta_m=item["proposed_observed_delta_m"],
            proposed_distance_m=item["proposed_distance_m"],
            detail=item["detail"],
            issues=list(item["issues"]),
        )
        for item in preview["rows"]
    ]

    if strategy == RevisionStrategy.ALL_OR_NOTHING and preview["blocking_rows"]:
        return {
            "project_id": project_id,
            "strategy": str(strategy),
            "applied": False,
            "total_rows": preview["total_rows"],
            "applied_rows": 0,
            "blocking_rows": preview["blocking_rows"],
            "receipts": [r.as_dict() for r in receipts],
            "detail": "存在版本冲突/缺失记录/重复行/非法行，整批未应用",
        }

    applied = 0
    for item in receipts:
        if item.state != APPLICABLE:
            continue
        obs = db.scalar(
            select(Observation).where(
                Observation.project_id == project_id, Observation.line_code == item.line_code
            )
        )
        if obs is None:  # vanished between preview and apply
            item.state = MISSING_RECORD
            item.detail = f"项目 {project_id} 中找不到测段 {item.line_code!r}"
            continue
        changes = {
            "observed_delta_m": item.proposed_observed_delta_m,
            "distance_m": item.proposed_distance_m,
        }
        if strategy == RevisionStrategy.PER_ROW:
            savepoint = db.begin_nested()
        try:
            updated, audit = apply_optimistic_update_with_audit(
                db,
                obs,
                changes,
                expected_version=int(item.expected_lock_version),
                actor=actor,
                action="csv_revision",
            )
        except StaleDraftError as exc:
            # Another surveyor changed the draft between preview and confirm.
            if strategy == RevisionStrategy.PER_ROW:
                savepoint.rollback()
            item.state = VERSION_CONFLICT
            item.current_lock_version = exc.detail["actual_lock_version"]
            item.new_lock_version = None
            item.audit_event_id = None
            item.detail = (
                f"锁版本冲突：文件期望 v{item.expected_lock_version}，当前草稿为 "
                f"v{item.current_lock_version}"
            )
            continue
        applied += 1
        item.current_lock_version = int(updated.lock_version) - 1
        item.new_lock_version = int(updated.lock_version)
        item.audit_event_id = audit.id
        item.detail = "修订已应用（审计链 lock_version_in -> lock_version_out）"
        # Keep echo consistent with the now-current draft.
        item.current_observed_delta_m = float(updated.observed_delta_m)
        item.current_distance_m = float(updated.distance_m)
        item.issues = []

    counts = {
        APPLICABLE: 0,
        ALREADY_APPLIED: 0,
        VERSION_CONFLICT: 0,
        MISSING_RECORD: 0,
        DUPLICATE_ROW: 0,
        INVALID_ROW: 0,
        "applied": 0,
    }
    for item in receipts:
        counts[item.state] = counts.get(item.state, 0) + 1
    counts["applied"] = applied
    return {
        "project_id": project_id,
        "strategy": str(strategy),
        "applied": applied > 0,
        "total_rows": len(receipts),
        "applied_rows": applied,
        "already_applied_rows": counts[ALREADY_APPLIED],
        "blocking_rows": sum(counts.get(state, 0) for state in BLOCKING_STATES),
        "counts": counts,
        "receipts": [r.as_dict() for r in receipts],
    }
