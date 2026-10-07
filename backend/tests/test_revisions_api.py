"""HTTP-level tests for the revision preview/confirm endpoints.

The database dependency is replaced by a minimal in-memory session; TestClient
is used without a lifespan context, so app startup (which would need Postgres)
never runs. The acceptance flow is exercised end to end: a file containing one
stale-version row and one valid revision produces clear per-row results, and
re-confirming the same file does not rewrite values.
"""

import io

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.main import app
from app.models.schema import AuditEvent, Observation, Project

HEADER = "line_code,lock_version,observed_delta_m,distance_m\n"


class RouteFakeSession:
    """Supports exactly what the revision routes use: get/scalars/add/flush/commit."""

    def __init__(self, project, observations):
        self._project = project
        self._observations = observations
        self.added = []
        self.commits = 0

    def get(self, model, entity_id):
        if model is Project and self._project is not None and entity_id == self._project.id:
            return self._project
        return None

    def scalars(self, _statement):
        class _Result:
            def __init__(self, items):
                self._items = items

            def all(self):
                return self._items

        return _Result(list(self._observations))

    def add(self, instance):
        self.added.append(instance)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1


@pytest.fixture()
def client_and_db():
    project = Project(id=1, code="P1", name="demo", lock_version=1)
    observations = [
        Observation(
            id=1,
            project_id=1,
            line_code="L-100",
            from_point_id=1,
            to_point_id=2,
            observed_delta_m=10.0,
            distance_m=1000.0,
            direction="forward",
            active=True,
            lock_version=2,
        ),
        Observation(
            id=2,
            project_id=1,
            line_code="L-200",
            from_point_id=2,
            to_point_id=3,
            observed_delta_m=20.0,
            distance_m=800.0,
            direction="forward",
            active=True,
            lock_version=2,
        ),
    ]
    db = RouteFakeSession(project, observations)
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), db, observations
    app.dependency_overrides.clear()


def upload(text: str):
    return {"file": ("revisions.csv", io.BytesIO(text.encode("utf-8")), "text/csv")}


def test_preview_file_with_stale_and_valid_rows(client_and_db):
    client, _db, _observations = client_and_db
    csv_text = HEADER + "L-100,1,99.0,500.0\nL-200,2,88.0,600.0\n"

    response = client.post("/api/projects/1/revisions/preview", files=upload(csv_text))

    assert response.status_code == 200
    body = response.json()
    assert body["total_rows"] == 2
    assert body["counts"]["version_conflict"] == 1
    assert body["counts"]["applicable"] == 1
    rows = {row["line_code"]: row for row in body["rows"]}
    assert rows["L-100"]["status"] == "version_conflict"
    assert rows["L-100"]["current"]["lock_version"] == 2
    assert rows["L-200"]["status"] == "applicable"
    assert rows["L-200"]["requested"]["observed_delta_m"] == 88.0


def test_confirm_per_row_then_reconfirm_is_idempotent(client_and_db):
    client, db, observations = client_and_db
    csv_text = HEADER + "L-100,1,99.0,500.0\nL-200,2,88.0,600.0\n"

    first = client.post(
        "/api/projects/1/revisions/confirm", files=upload(csv_text), data={"strategy": "per_row"}
    )
    assert first.status_code == 200
    receipt = first.json()
    assert receipt["applied_count"] == 1
    assert receipt["draft_changed"] is True
    rows = {row["line_code"]: row for row in receipt["rows"]}
    assert rows["L-100"]["status"] == "version_conflict"
    assert rows["L-200"]["status"] == "applied"
    assert rows["L-200"]["lock_version_after"] == 3
    assert float(observations[1].observed_delta_m) == 88.0
    assert observations[1].lock_version == 3
    assert float(observations[0].observed_delta_m) == 10.0  # stale row not overwritten
    assert any(isinstance(event, AuditEvent) for event in db.added)
    assert db.commits == 1

    # Same file a second time: the previously applied row now conflicts on
    # lock_version, so nothing is rewritten.
    second = client.post(
        "/api/projects/1/revisions/confirm", files=upload(csv_text), data={"strategy": "per_row"}
    )
    assert second.status_code == 200
    again = second.json()
    assert again["applied_count"] == 0
    assert again["draft_changed"] is False
    assert all(row["status"] == "version_conflict" for row in again["rows"])
    assert float(observations[1].observed_delta_m) == 88.0
    assert observations[1].lock_version == 3


def test_confirm_all_or_nothing_rejects_mixed_file(client_and_db):
    client, _db, observations = client_and_db
    csv_text = HEADER + "L-100,1,99.0,500.0\nL-200,2,88.0,600.0\n"

    response = client.post(
        "/api/projects/1/revisions/confirm",
        files=upload(csv_text),
        data={"strategy": "all_or_nothing"},
    )

    assert response.status_code == 200
    receipt = response.json()
    assert receipt["applied_count"] == 0
    assert receipt["draft_changed"] is False
    rows = {row["line_code"]: row for row in receipt["rows"]}
    assert rows["L-100"]["status"] == "version_conflict"
    assert rows["L-200"]["status"] == "blocked_by_strategy"
    assert float(observations[1].observed_delta_m) == 20.0
    assert observations[1].lock_version == 2


def test_confirm_rejects_bad_strategy_and_unknown_project(client_and_db):
    client, _db, _observations = client_and_db
    csv_text = HEADER + "L-200,2,88.0,600.0\n"

    bad_strategy = client.post(
        "/api/projects/1/revisions/confirm", files=upload(csv_text), data={"strategy": "yolo"}
    )
    assert bad_strategy.status_code == 400

    missing_project = client.post("/api/projects/999/revisions/preview", files=upload(csv_text))
    assert missing_project.status_code == 404

    bad_columns = client.post(
        "/api/projects/1/revisions/preview",
        files=upload("line_code,observed_delta_m\nL-200,1.0\n"),
    )
    assert bad_columns.status_code == 400
