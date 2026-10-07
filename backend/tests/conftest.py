"""Pytest configuration.

Production runs on PostgreSQL/PostGIS. The test environment in CI may not have
SpatiaLite, so before DDL is emitted on SQLite the Geometry columns are swapped
for plain Text (the geometry column is unused by the revision/snapshot logic
under test). CheckConstraints, JSONB and optimistic-lock behaviour are unchanged.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import Text

from app.core.db import Base
from app.models import schema as schema_module  # noqa: F401  (register tables)
from app.models.schema import Observation, Point, Project, WeightRule
from app.services.snapshots import create_immutable_snapshot, ensure_single_generation


@pytest.fixture(scope="session", autouse=True)
def _sqlite_geometry_shim() -> None:
    from geoalchemy2 import Geometry

    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, Geometry):
                column.type = Text()
            elif isinstance(column.type, JSONB):
                column.type = JSON()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    connection = engine.connect()
    from sqlalchemy.orm import Session

    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        connection.close()
        engine.dispose()


@pytest.fixture
def project_with_observations(db):
    """Draft project with two observations, both at lock_version 1, plus a weight rule."""
    project = Project(code="DEMO", name="测段修订验收")
    db.add(project)
    db.flush()
    p_a = Point(project_id=project.id, code="A")
    p_b = Point(project_id=project.id, code="B")
    p_c = Point(project_id=project.id, code="C")
    db.add_all([p_a, p_b, p_c])
    db.flush()
    obs1 = Observation(
        project_id=project.id,
        line_code="L-001",
        from_point_id=p_a.id,
        to_point_id=p_b.id,
        observed_delta_m=Decimal("1.001000000"),
        distance_m=Decimal("1000.000"),
    )
    obs2 = Observation(
        project_id=project.id,
        line_code="L-002",
        from_point_id=p_b.id,
        to_point_id=p_c.id,
        observed_delta_m=Decimal("2.000000000"),
        distance_m=Decimal("1500.500"),
    )
    db.add_all(
        [
            obs1,
            obs2,
            WeightRule(
                project_id=project.id,
                name="default",
                rule={"method": "distance_inverse_km", "c_km": 1.0, "base_sigma_m": 0.001},
            ),
        ]
    )
    db.commit()
    return {"project_id": project.id, "line_codes": ["L-001", "L-002"]}


@pytest.fixture
def snapshot_job(db, project_with_observations):
    """An immutable snapshot + job bound to the *original* draft values."""
    project_id = project_with_observations["project_id"]
    snapshot = create_immutable_snapshot(db, project_id)
    job, _ = ensure_single_generation(db, project_id, snapshot.id)
    # Mark the job completed as if the old run finished normally.
    from app.models.schema import JobStatus, StageStatus

    job.status = JobStatus.COMPLETED
    for stage in job.stages:
        stage.status = StageStatus.CONFIRMED
        stage.detail = {"regularization": {"value": "none", "passed": True}}
    db.commit()
    return {"job_id": job.id, "snapshot_id": snapshot.id, "project_id": project_id}
