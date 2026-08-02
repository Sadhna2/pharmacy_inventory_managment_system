"""Postgres-backed job queue (ARCHITECTURE.md §8).

No Redis, no Celery. Workers claim rows with FOR UPDATE SKIP LOCKED, which
means multiple workers never collide. Layer 0/1 does not need workers, but the
table exists so Layer 2 can bolt on without a migration.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import JobStatus


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # Partial index: only QUEUED rows are ever scanned by the claim loop.
        Index(
            "ix_jobs_claim",
            "status",
            "priority",
            "run_after",
            postgresql_where=text("status = 'QUEUED'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), default=JobStatus.QUEUED, nullable=False
    )
    priority: Mapped[int] = mapped_column(SmallInteger, default=5, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Makes double-enqueue impossible for scheduled jobs.
    dedupe_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
