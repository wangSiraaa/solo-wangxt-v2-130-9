"""Acceptance tests for the CSV observation revision preview/confirm flow.

Covers:
- preview classification: applicable / version_conflict / missing / duplicate /
  invalid / already_applied on a file containing one stale and one legal row;
- per-row and all-or-nothing confirmation strategies with row-level receipts;
- idempotency: re-confirming the same file never changes values twice;
- draft lock_version advances through the existing audit chain;
- immutable snapshots and the old job remain audit-only after a revision, and
  creating a new draft snapshot yields a new version.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.schema import AuditEvent, Job, Observation, Snapshot
from app.services.revisions import (
    ALREADY_APPLIED,
    APPLICABLE,
    DUPLICATE_ROW,
    INVALID_ROW,
    MISSING_RECORD,
    VERSION_CONFLICT,
    RevisionSheetError,
    RevisionStrategy,
    confirm_observation_revisions,
    preview_observation_revisions,
)
from app.services.snapshots import create_immutable_snapshot

CSV_HEADER = "line_code,lock_version,observed_delta_m,distance_m\n"


def _csv(body: str) -> bytes:
    return (CSV_HEADER + body).encode("utf-8")


# One stale version (L-001 expects v7 while draft is v1) and one legal revision.
MIXED_CSV = _csv(
    "L-001,7,1.010000000,1000.000\n"
    "L-002,1,2.005000000,1500.500\n"
)


def _get(db, line_code: str) -> Observation:
    return db.scalar(select(Observation).where(Observation.line_code == line_code))


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #


def test_preview_classifies_stale_and_legal_rows(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    result = preview_observation_revisions(db, project_id, MIXED_CSV)

    assert result["total_rows"] == 2
    assert result["applicable_rows"] == 1
    assert result["blocking_rows"] == 1
    assert result["ready_to_apply"] is False

    states = {row["line_code"]: row["state"] for row in result["rows"]}
    assert states["L-001"] == VERSION_CONFLICT
    assert states["L-002"] == APPLICABLE

    stale = next(row for row in result["rows"] if row["line_code"] == "L-001")
    assert stale["expected_lock_version"] == 7
    assert stale["current_lock_version"] == 1
    assert stale["proposed_observed_delta_m"] == pytest.approx(1.01)

    legal = next(row for row in result["rows"] if row["line_code"] == "L-002")
    assert legal["new_lock_version"] == 2
    assert legal["current_observed_delta_m"] == pytest.approx(2.0)

    # Preview is strictly read-only.
    db.expire_all()
    assert _get(db, "L-001").lock_version == 1
    assert Decimal(_get(db, "L-002").observed_delta_m) == Decimal("2.000000000")


def test_preview_reports_missing_duplicate_and_invalid_rows(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    content = _csv(
        "L-002,1,2.01,1500.500\n"          # applicable
        "L-002,1,2.02,1500.500\n"          # duplicate line_code
        "L-999,1,1.0,500\n"                # missing record
        "L-001,1,not-a-number,1000\n"      # invalid delta
        "L-001,1,1.0,0\n"                  # duplicate id, also non-positive length
    )
    result = preview_observation_revisions(db, project_id, content)
    states = [row["state"] for row in result["rows"]]
    assert states == [APPLICABLE, DUPLICATE_ROW, MISSING_RECORD, INVALID_ROW, INVALID_ROW]
    invalid = result["rows"][3]
    assert any("高差" in issue for issue in invalid["issues"])
    invalid_length = result["rows"][4]
    assert any("长度" in issue for issue in invalid_length["issues"])
    assert result["counts"][APPLICABLE] == 1
    assert result["counts"][DUPLICATE_ROW] == 1
    assert result["counts"][MISSING_RECORD] == 1
    assert result["counts"][INVALID_ROW] == 2


def test_preview_recognizes_chinese_headers(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    content = "测段编号,原锁版本,高差,长度\nL-002,1,2.005,1500.5\n".encode("utf-8")
    result = preview_observation_revisions(db, project_id, content)
    assert result["rows"][0]["state"] == APPLICABLE


def test_preview_rejects_bad_header_and_empty_sheet(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    with pytest.raises(RevisionSheetError):
        preview_observation_revisions(db, project_id, b"line_code,lock_version\nL-001,1\n")
    with pytest.raises(RevisionSheetError):
        preview_observation_revisions(db, project_id, b"")


def test_preview_marks_no_change_sheet_as_already_applied(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    content = _csv("L-001,1,1.001,1000\n")
    result = preview_observation_revisions(db, project_id, content)
    assert result["rows"][0]["state"] == ALREADY_APPLIED
    assert result["ready_to_apply"] is True  # nothing blocking


# --------------------------------------------------------------------------- #
# Confirm: all-or-nothing
# --------------------------------------------------------------------------- #


def test_all_or_nothing_applies_when_all_rows_legal(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    content = _csv("L-002,1,2.005000000,1500.500\n")
    result = confirm_observation_revisions(db, project_id, content)
    assert result["applied"] is True
    assert result["applied_rows"] == 1
    receipt = result["receipts"][0]
    assert receipt["state"] == APPLICABLE
    assert receipt["new_lock_version"] == 2
    assert receipt["audit_event_id"] is not None

    db.commit()
    obs = _get(db, "L-002")
    assert obs.lock_version == 2
    assert Decimal(obs.observed_delta_m) == Decimal("2.005000000")


def test_all_or_nothing_with_stale_row_changes_nothing(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    result = confirm_observation_revisions(db, project_id, MIXED_CSV)
    assert result["applied"] is False
    assert result["applied_rows"] == 0
    assert result["blocking_rows"] == 1
    receipts = {r["line_code"]: r["state"] for r in result["receipts"]}
    assert receipts == {"L-001": VERSION_CONFLICT, "L-002": APPLICABLE}

    # Even the legal row must not be applied in all-or-nothing.
    db.commit()
    db.expire_all()
    obs1, obs2 = _get(db, "L-001"), _get(db, "L-002")
    assert obs1.lock_version == 1
    assert obs2.lock_version == 1
    assert Decimal(obs2.observed_delta_m) == Decimal("2.000000000")
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


# --------------------------------------------------------------------------- #
# Confirm: per-row
# --------------------------------------------------------------------------- #


def test_per_row_applies_legal_row_and_reports_conflict(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    result = confirm_observation_revisions(db, project_id, MIXED_CSV, strategy=RevisionStrategy.PER_ROW)
    assert result["applied"] is True
    assert result["applied_rows"] == 1
    assert result["blocking_rows"] == 1

    by_line = {r["line_code"]: r for r in result["receipts"]}
    assert by_line["L-001"]["state"] == VERSION_CONFLICT
    assert by_line["L-002"]["state"] == APPLICABLE
    assert by_line["L-002"]["new_lock_version"] == 2

    db.commit()
    db.expire_all()
    obs1, obs2 = _get(db, "L-001"), _get(db, "L-002")
    assert obs1.lock_version == 1
    assert Decimal(obs1.observed_delta_m) == Decimal("1.001000000")
    assert obs2.lock_version == 2
    assert Decimal(obs2.observed_delta_m) == Decimal("2.005000000")

    # The legal revision goes through the existing optimistic audit chain.
    audit = db.scalar(
        select(AuditEvent).where(AuditEvent.entity_type == "observation", AuditEvent.entity_id == obs2.id)
    )
    assert audit.action == "csv_revision"
    assert audit.lock_version_in == 1
    assert audit.lock_version_out == 2
    assert audit.before["observed_delta_m"] == pytest.approx(2.0)
    assert audit.after["observed_delta_m"] == pytest.approx(2.005)


def test_per_row_skips_missing_duplicate_and_invalid_rows(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    content = _csv(
        "L-002,1,2.01,1500.500\n"
        "L-002,1,2.02,1500.500\n"
        "L-999,1,1.0,500\n"
        ",1,1.0,500\n"
    )
    result = confirm_observation_revisions(db, project_id, content, strategy=RevisionStrategy.PER_ROW)
    assert result["applied_rows"] == 1
    states = [r["state"] for r in result["receipts"]]
    assert states == [APPLICABLE, DUPLICATE_ROW, MISSING_RECORD, INVALID_ROW]
    db.commit()
    assert _get(db, "L-002").lock_version == 2
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_reconfirming_same_file_does_not_change_value_again(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    content = _csv("L-002,1,2.005000000,1500.500\n")

    first = confirm_observation_revisions(db, project_id, content, strategy=RevisionStrategy.PER_ROW)
    db.commit()
    assert first["applied_rows"] == 1
    assert _get(db, "L-002").lock_version == 2

    # Same file, same (now stale) expected version.
    second = confirm_observation_revisions(db, project_id, content, strategy=RevisionStrategy.PER_ROW)
    db.commit()
    assert second["applied_rows"] == 0
    receipt = second["receipts"][0]
    # Values already match the sheet: idempotent no-op, never a second overwrite.
    assert receipt["state"] == ALREADY_APPLIED
    assert receipt["audit_event_id"] is None

    db.expire_all()
    obs = _get(db, "L-002")
    assert obs.lock_version == 2
    assert Decimal(obs.observed_delta_m) == Decimal("2.005000000")
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_unchanged_value_even_when_version_differs_is_not_overwritten(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    # Advance L-001 through the normal PATCH path, then resend a sheet carrying
    # the old version but the *same* values.
    from app.services.snapshots import apply_optimistic_update

    obs = _get(db, "L-001")
    apply_optimistic_update(db, obs, {"distance_m": Decimal("1001.000")}, expected_version=1)
    db.commit()
    assert obs.lock_version == 2

    content = _csv("L-001,1,1.001,1000\n")  # stale version, original values
    result = preview_observation_revisions(db, project_id, content)
    row = result["rows"][0]
    # Values don't match current draft either; still a conflict, never silent overwrite.
    assert row["state"] == VERSION_CONFLICT


# --------------------------------------------------------------------------- #
# Snapshot / old-job invariants
# --------------------------------------------------------------------------- #


def test_revision_creates_new_draft_snapshot_but_leaves_old_snapshot_and_job(
    db, project_with_observations, snapshot_job
):
    project_id = snapshot_job["project_id"]
    old_job_id = snapshot_job["job_id"]
    old_snapshot_id = snapshot_job["snapshot_id"]

    old_snapshot = db.get(Snapshot, old_snapshot_id)
    old_obs_payload = [dict(row) for row in old_snapshot.payload["observations"]]
    old_hash = old_snapshot.observations_sha256
    old_version = old_snapshot.version

    # Apply a legal field revision.
    content = _csv("L-002,1,2.005000000,1500.500\n")
    confirm_observation_revisions(db, project_id, content, strategy=RevisionStrategy.PER_ROW)
    db.commit()

    # Old snapshot payload/hash is byte-for-byte unchanged and still immutable.
    db.expire_all()
    old_snapshot = db.get(Snapshot, old_snapshot_id)
    assert old_snapshot.observations_sha256 == old_hash
    assert old_snapshot.version == old_version
    assert old_snapshot.immutable is True
    assert [
        (row["line_code"], row["observed_delta_m"], row["distance_m"]) for row in old_snapshot.payload["observations"]
    ] == [(row["line_code"], row["observed_delta_m"], row["distance_m"]) for row in old_obs_payload]

    # The new draft gets a *new* snapshot version.
    new_snapshot = create_immutable_snapshot(db, project_id)
    db.commit()
    assert new_snapshot.id != old_snapshot_id
    assert new_snapshot.version == old_version + 1
    assert new_snapshot.observations_sha256 != old_hash

    # Resubmitting the unchanged new draft deduplicates instead of forking.
    again = create_immutable_snapshot(db, project_id)
    assert again.id == new_snapshot.id

    # The old job is still bound to the old snapshot: audit-only, never published.
    old_job = db.get(Job, old_job_id)
    assert old_job.snapshot_id == old_snapshot_id
    assert latest_snapshot_id(db, project_id) == new_snapshot.id


def latest_snapshot_id(db, project_id: int) -> int:
    return db.scalar(select(func.max(Snapshot.id)).where(Snapshot.project_id == project_id))


def test_old_job_cannot_publish_after_revision(db, project_with_observations, snapshot_job):
    from app.api.routes import publish
    from app.api.schemas import PublishIn

    project_id = snapshot_job["project_id"]
    old_job_id = snapshot_job["job_id"]

    content = _csv("L-002,1,2.005000000,1500.500\n")
    confirm_observation_revisions(db, project_id, content, strategy=RevisionStrategy.PER_ROW)
    create_immutable_snapshot(db, project_id)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        publish(old_job_id, PublishIn(confirm=True), db)
    assert exc_info.value.status_code == 409
    assert "stale generation" in exc_info.value.detail
    # Nothing was published; the old job stays an audit record.
    from app.models.schema import Publication

    assert db.scalar(select(func.count()).select_from(Publication)) == 0
