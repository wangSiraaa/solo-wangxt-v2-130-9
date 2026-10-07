"""HTTP-level tests for the CSV revision upload endpoints (multipart)."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import get_db
from app.main import app
from app.models.schema import AuditEvent, Observation


def _client(db) -> TestClient:
    # Constructing without entering context avoids triggering the PostGIS startup
    # event; tables already exist in the test session.
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    return client


CSV = "line_code,lock_version,observed_delta_m,distance_m\n".encode()


def test_preview_endpoint_reports_stale_and_legal_rows(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    files = {"file": ("revisions.csv", CSV + b"L-001,7,1.01,1000\nL-002,1,2.005,1500.5\n", "text/csv")}
    response = _client(db).post(f"/api/projects/{project_id}/observations/revisions/preview", files=files)
    assert response.status_code == 200
    data = response.json()
    assert data["applicable_rows"] == 1
    assert data["blocking_rows"] == 1
    assert data["ready_to_apply"] is False
    states = {row["line_code"]: row["state"] for row in data["rows"]}
    assert states == {"L-001": "version_conflict", "L-002": "applicable"}


def test_all_or_nothing_confirm_returns_409_and_writes_nothing(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    files = {"file": ("revisions.csv", CSV + b"L-001,7,1.01,1000\nL-002,1,2.005,1500.5\n", "text/csv")}
    response = _client(db).post(
        f"/api/projects/{project_id}/observations/revisions/confirm",
        data={"strategy": "all_or_nothing"},
        files=files,
    )
    assert response.status_code == 409
    body = response.json()["detail"]
    assert body["error"] == "revision_batch_blocked"
    assert body["blocking_rows"] == 1

    db.expire_all()
    obs2 = db.scalar(select(Observation).where(Observation.line_code == "L-002"))
    assert obs2.lock_version == 1
    assert Decimal(obs2.observed_delta_m) == Decimal("2.000000000")


def test_per_row_confirm_returns_row_receipts_and_is_idempotent(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    sheet = CSV + b"L-002,1,2.005,1500.5\n"

    client = _client(db)
    response = client.post(
        f"/api/projects/{project_id}/observations/revisions/confirm",
        data={"strategy": "per_row"},
        files={"file": ("revisions.csv", sheet, "text/csv")},
    )
    assert response.status_code == 200
    first = response.json()
    assert first["applied_rows"] == 1
    receipt = first["receipts"][0]
    assert receipt["state"] == "applicable"
    assert receipt["new_lock_version"] == 2
    assert receipt["audit_event_id"] is not None

    # Re-upload the identical file: no second value change, no extra audit row.
    response2 = client.post(
        f"/api/projects/{project_id}/observations/revisions/confirm",
        data={"strategy": "per_row"},
        files={"file": ("revisions.csv", sheet, "text/csv")},
    )
    assert response2.status_code == 200
    second = response2.json()
    assert second["applied_rows"] == 0
    assert second["receipts"][0]["state"] == "already_applied"

    db.expire_all()
    obs = db.scalar(select(Observation).where(Observation.line_code == "L-002"))
    assert obs.lock_version == 2
    assert Decimal(obs.observed_delta_m) == Decimal("2.005000000")
    audits = db.scalars(
        select(AuditEvent).where(AuditEvent.entity_type == "observation", AuditEvent.entity_id == obs.id)
    ).all()
    assert len(audits) == 1
    assert audits[0].action == "csv_revision"


def test_confirm_rejects_bad_csv_with_400(db, project_with_observations):
    project_id = project_with_observations["project_id"]
    files = {"file": ("bad.csv", b"not,a,csv\n1,2,3\n", "text/csv")}
    response = _client(db).post(f"/api/projects/{project_id}/observations/revisions/preview", files=files)
    assert response.status_code == 400
    assert "缺少必需列" in response.json()["detail"]
