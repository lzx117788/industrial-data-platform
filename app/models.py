from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():

    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    entity: Mapped[str] = mapped_column(String(16))
    config: Mapped[dict] = mapped_column(JSON)
    cursor_at: Mapped[datetime | None] = mapped_column(DateTime)
    cursor_id: Mapped[str | None] = mapped_column(String(128))
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    device_key: Mapped[str] = mapped_column(String(128), unique=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(64))
    location: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    source_updated_at: Mapped[datetime] = mapped_column(DateTime)
    payload_hash: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DeviceAlias(Base):
    __tablename__ = "device_aliases"
    __table_args__ = (UniqueConstraint("source_id", "external_device_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_device_id: Mapped[str] = mapped_column(String(128))
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)


class Alarm(Base):
    __tablename__ = "alarms"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        Index("ix_alarms_device_time", "device_id", "occurred_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(128))
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    code: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime)
    payload_hash: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Maintenance(Base):
    __tablename__ = "maintenance"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        Index("ix_maintenance_device_time", "device_id", "performed_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(128))
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    description: Mapped[str] = mapped_column(Text)
    technician: Mapped[str] = mapped_column(String(64))
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    performed_at: Mapped[datetime] = mapped_column(DateTime)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime)
    payload_hash: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    full_sync: Mapped[bool] = mapped_column(default=False)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    read_count: Mapped[int] = mapped_column(Integer, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    complete_count: Mapped[int] = mapped_column(Integer, default=0)
    relation_checked: Mapped[int] = mapped_column(Integer, default=0)
    relation_resolved: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[float | None]
    error: Mapped[str | None] = mapped_column(Text)


class QualityIssue(Base):
    __tablename__ = "quality_issues"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("sync_runs.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str | None] = mapped_column(String(128))
    code: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
