"""CSV revision preview/confirm tests.

The classification and receipt logic is exercised against real Observation ORM
instances; the fake session supplies only flush/add, which is all the shared
``apply_optimistic_update`` audit path consumes. No database is required.
"""

import pytest
from fastapi import HTTPException

from app.models.schema import AuditEvent, Job, JobStage, Observation, Snapshot
from app.services.revisions import (
    ApplyStrategy,
    RowStatus,
    apply_assessments,
    assess_csv,
    build_preview,
    parse_revision_csv,
)

HEADER = "line_code,lock_version,observed_delta_m,distance_m\n"


class FakeSession:
    """apply_optimistic_update only needs flush() and add()."""

    def __init__(self):
        self.added = []
        self.flush_calls = 0

    def add(self, instance):
        self.added.append(instance)

    def flush(self):
        self.flush_calls += 1


def make_obs(obs_id: int, line_code: str, lock_version: int, delta: float = 1.0, distance: float = 1000.0) -> Observation:
    return Observation(
        id=obs_id,
        project_id=1,
        line_code=line_code,
        from_point_id=1,
        to_point_id=2,
        observed_delta_m=delta,
        distance_m=distance,
        direction="forward",
        active=True,
        lock_version=lock_version,
    )


def status_by_line(preview: dict) -> dict[str, str]:
    return {row["line_code"]: row["status"] for row in preview["rows"]}


def test_preview_reports_applicable_conflict_missing_duplicate_and_invalid():
    observations = {
        "L-100": make_obs(1, "L-100", lock_version=2),  # file says 1 -> stale
        "L-200": make_obs(2, "L-200", lock_version=2),  # file says 2 -> applicable
        "L-300": make_obs(3, "L-300", lock_version=1),  # duplicated in file
    }
    csv_text = (
        HEADER
        + "L-100,1,12.3456,1250.5\n"
        + "L-200,2,-0.8234,980.0\n"
        + "L-999,1,0.5,100.0\n"
        + "L-300,1,1.0,100.0\n"
        + "L-300,1,2.0,200.0\n"
        + "L-400,1,0.1,-5\n"
    )
    preview = build_preview(1, assess_csv(csv_text, observations))

    statuses = [row["status"] for row in preview["rows"]]
    assert statuses == [
        "version_conflict",  # L-100 stale lock_version
        "applicable",  # L-200
        "missing_record",  # L-999 unknown
        "applicable",  # L-300 first occurrence wins
        "duplicate_line",  # L-300 second occurrence
        "invalid_row",  # L-400 negative distance
    ]
    rows = {row["line_code"]: row for row in preview["rows"] if row["status"] != "duplicate_line"}
    assert preview["counts"] == {
        "applicable": 2,
        "version_conflict": 1,
        "missing_record": 1,
        "duplicate_line": 1,
        "invalid_row": 1,
    }
    conflict = rows["L-100"]
    assert conflict["current"]["lock_version"] == 2
    assert conflict["requested"] == {"lock_version": 1, "observed_delta_m": 12.3456, "distance_m": 1250.5}
    applicable = rows["L-200"]
    assert applicable["observation_id"] == 2
    assert applicable["current"]["observed_delta_m"] == 1.0


def test_all_or_nothing_rejects_whole_file_without_touching_draft():
    observations = {
        "L-100": make_obs(1, "L-100", lock_version=2, delta=10.0),
        "L-200": make_obs(2, "L-200", lock_version=2, delta=20.0),
    }
    csv_text = HEADER + "L-100,1,99.0,500.0\nL-200,2,88.0,600.0\n"
    db = FakeSession()

    receipt = apply_assessments(
        db, 1, assess_csv(csv_text, observations), strategy=ApplyStrategy.ALL_OR_NOTHING
    )

    assert receipt["applied_count"] == 0
    assert receipt["draft_changed"] is False
    assert status_by_line(receipt) == {"L-100": "version_conflict", "L-200": "blocked_by_strategy"}
    # Nothing written: draft values and lock versions untouched, no audit rows.
    assert float(observations["L-100"].observed_delta_m) == 10.0
    assert float(observations["L-200"].observed_delta_m) == 20.0
    assert observations["L-200"].lock_version == 2
    assert db.added == []


def test_per_row_applies_valid_rows_and_skips_conflicts_with_audit():
    observations = {
        "L-100": make_obs(1, "L-100", lock_version=2, delta=10.0),
        "L-200": make_obs(2, "L-200", lock_version=2, delta=20.0, distance=800.0),
    }
    csv_text = HEADER + "L-100,1,99.0,500.0\nL-200,2,88.0,600.0\n"
    db = FakeSession()

    receipt = apply_assessments(db, 1, assess_csv(csv_text, observations), strategy=ApplyStrategy.PER_ROW)

    assert receipt["applied_count"] == 1
    assert receipt["skipped_count"] == 1
    assert receipt["draft_changed"] is True
    rows = {row["line_code"]: row for row in receipt["rows"]}
    assert rows["L-100"]["status"] == "version_conflict"
    assert rows["L-100"]["current_lock_version"] == 2
    applied = rows["L-200"]
    assert applied["status"] == "applied"
    assert applied["lock_version_before"] == 2
    assert applied["lock_version_after"] == 3

    # Draft row updated through the optimistic-lock path; stale row untouched.
    assert float(observations["L-200"].observed_delta_m) == 88.0
    assert float(observations["L-200"].distance_m) == 600.0
    assert observations["L-200"].lock_version == 3
    assert float(observations["L-100"].observed_delta_m) == 10.0
    assert observations["L-100"].lock_version == 2

    # Exactly one audit event, with the before/after chain required by audit semantics.
    audits = [event for event in db.added if isinstance(event, AuditEvent)]
    assert len(audits) == 1
    audit = audits[0]
    assert audit.entity_type == "observation"
    assert audit.entity_id == 2
    assert audit.expected_version == 2
    assert audit.lock_version_in == 2
    assert audit.lock_version_out == 3
    assert audit.before["observed_delta_m"] == 20.0
    assert audit.after["observed_delta_m"] == 88.0


def test_reconfirming_same_file_does_not_change_values_twice():
    observations = {"L-200": make_obs(2, "L-200", lock_version=2, delta=20.0)}
    csv_text = HEADER + "L-200,2,88.0,600.0\n"
    db = FakeSession()

    first = apply_assessments(db, 1, assess_csv(csv_text, observations), strategy=ApplyStrategy.PER_ROW)
    assert first["applied_count"] == 1
    assert observations["L-200"].lock_version == 3

    # Same file again: the declared lock_version is now stale, so the row is a
    # conflict and the values are not rewritten.
    second = apply_assessments(db, 1, assess_csv(csv_text, observations), strategy=ApplyStrategy.PER_ROW)
    assert second["applied_count"] == 0
    assert second["draft_changed"] is False
    assert second["rows"][0]["status"] == "version_conflict"
    assert float(observations["L-200"].observed_delta_m) == 88.0
    assert observations["L-200"].lock_version == 3
    assert len([event for event in db.added if isinstance(event, AuditEvent)]) == 1


def test_confirm_never_touches_snapshots_or_jobs():
    observations = {"L-200": make_obs(2, "L-200", lock_version=2)}
    csv_text = HEADER + "L-200,2,88.0,600.0\n"
    db = FakeSession()

    apply_assessments(db, 1, assess_csv(csv_text, observations), strategy=ApplyStrategy.ALL_OR_NOTHING)

    # The revision path writes draft observations plus audit events only; old
    # snapshots and jobs are immutable history and must not appear here.
    forbidden = (Snapshot, Job, JobStage)
    assert not any(isinstance(event, forbidden) for event in db.added)
    assert all(isinstance(event, AuditEvent) for event in db.added)


def test_parse_rejects_structural_problems():
    with pytest.raises(HTTPException) as excinfo:
        parse_revision_csv("")
    assert excinfo.value.status_code == 400
    with pytest.raises(HTTPException) as excinfo:
        parse_revision_csv("line_code,observed_delta_m\nL-1,1.0\n")
    assert excinfo.value.status_code == 400
    assert "lock_version" in excinfo.value.detail


def test_parse_flags_malformed_rows_as_invalid():
    parsed = parse_revision_csv(
        HEADER
        + "L-1,not-an-int,1.0,100\n"
        + "L-2,1,abc,100\n"
        + "L-3,1,1.0,0\n"
        + "L-4,0,1.0,100\n"
        + ",1,1.0,100\n"
        + "L-6,2,1.25,250.5\n"
    )
    kinds = [type(row).__name__ for row in parsed]
    assert kinds == ["InvalidRow"] * 5 + ["ParsedRow"]
    assert parsed[4].line_code is None  # blank line_code
    valid = parsed[5]
    assert valid.row == 6 and valid.line_code == "L-6" and valid.lock_version == 2


def test_duplicate_first_occurrence_wins_on_apply():
    observations = {"L-300": make_obs(3, "L-300", lock_version=1, delta=5.0)}
    csv_text = HEADER + "L-300,1,7.0,700.0\nL-300,1,9.0,900.0\n"
    db = FakeSession()

    receipt = apply_assessments(db, 1, assess_csv(csv_text, observations), strategy=ApplyStrategy.PER_ROW)

    assert [row["status"] for row in receipt["rows"]] == ["applied", "duplicate_line"]
    assert float(observations["L-300"].observed_delta_m) == 7.0
    assert observations["L-300"].lock_version == 2


def test_assessments_cover_every_row_status_enum_value():
    # Guard the report vocabulary stays in sync with the enum used by the API.
    assert {status.value for status in RowStatus} == {
        "applicable",
        "version_conflict",
        "missing_record",
        "duplicate_line",
        "invalid_row",
    }
