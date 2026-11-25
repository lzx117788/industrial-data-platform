import hashlib
import json
import time
import uuid
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.adapters import fetch_records
from app.config import Settings
from app.db import Database
from app.models import (
    Alarm,
    Device,
    DeviceAlias,
    Maintenance,
    QualityIssue,
    Source,
    SyncRun,
    utcnow,
)
from app.schemas import RECORD_TYPES, parse_time


class SourceBusy(RuntimeError):
    pass


class InjectedFailure(RuntimeError):
    """Used only by the recovery verification script and automated tests."""


class RecordError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def record_cursor(row):
    try:
        external_id = str(row["external_id"]).strip()
        if not external_id:
            return None
        return parse_time(row["updated_at"]), external_id
    except (ValueError, TypeError, KeyError):
        return None


def digest(record):
    canonical = json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def upsert_record(session, source, record, run):
    entity = source.entity
    model = {"device": Device, "alarm": Alarm, "maintenance": Maintenance}[entity]
    existing = session.scalar(
        select(model).where(model.source_id == source.id, model.external_id == record.external_id)
    )
    payload_hash = digest(record)
    if existing:
        if record.updated_at < existing.source_updated_at:
            return "unchanged"
        if record.updated_at == existing.source_updated_at:
            if existing.payload_hash == payload_hash:
                return "unchanged"
            raise RecordError(
                "version_conflict",
                "Same record ID and timestamp have different contents; advance updated_at",
            )

    values = record.model_dump(exclude={"updated_at", "aliases", "device_ref"})
    if entity == "device":
        values["device_key"] = record.external_id
    else:
        run.relation_checked += 1
        alias = session.scalar(
            select(DeviceAlias).where(
                DeviceAlias.source_id == source.id,
                DeviceAlias.external_device_id == record.device_ref,
            )
        )
        if alias is None:
            raise RecordError(
                "unmapped_device", f"No device mapping for {source.code}/{record.device_ref}"
            )
        run.relation_resolved += 1
        values["device_id"] = alias.device_id
    values.update(
        source_id=source.id,
        source_updated_at=record.updated_at,
        payload_hash=payload_hash,
        ingested_at=utcnow(),
    )
    if existing:
        for key, value in values.items():
            setattr(existing, key, value)
        target = existing
        outcome = "updated"
    else:
        target = model(**values)
        session.add(target)
        session.flush()
        outcome = "inserted"

    if entity == "device":
        aliases = {**record.aliases, source.code: record.external_id}
        for code, external in aliases.items():
            alias_source = session.scalar(select(Source).where(Source.code == code))
            if alias_source is None:
                raise RecordError("unknown_alias_source", f"Unknown alias source: {code}")
            alias = session.scalar(
                select(DeviceAlias).where(
                    DeviceAlias.source_id == alias_source.id,
                    DeviceAlias.external_device_id == external,
                )
            )
            if alias and alias.device_id != target.id:
                raise RecordError(
                    "mapping_conflict", f"Alias {code}/{external} belongs to another device"
                )
            if not alias:
                session.add(
                    DeviceAlias(
                        source_id=alias_source.id, external_device_id=external, device_id=target.id
                    )
                )
        session.flush()
    return outcome


class SyncService:
    def __init__(self, db: Database, settings: Settings, http_transport=None):
        self.db, self.settings, self.http_transport = db, settings, http_transport

    def sync(self, source_id: int, full=False, fail_after_batches: int | None = None):
        token = str(uuid.uuid4())
        start = time.perf_counter()
        with self.db.session() as session:
            if session.get(Source, source_id) is None:
                raise LookupError("Source not found")
            now = utcnow()
            claim = session.execute(
                update(Source)
                .where(
                    Source.id == source_id,
                    or_(Source.lease_expires_at.is_(None), Source.lease_expires_at < now),
                )
                .values(
                    lease_owner=token,
                    lease_expires_at=now + timedelta(seconds=self.settings.lease_seconds),
                )
            )
            if claim.rowcount != 1:
                session.rollback()
                raise SourceBusy(
                    "Source is already syncing; retry after it finishes or its lease expires"
                )
            session.execute(
                update(SyncRun)
                .where(SyncRun.source_id == source_id, SyncRun.status == "running")
                .values(status="abandoned", finished_at=now, error="Previous worker lease expired")
            )
            session.add(SyncRun(id=token, source_id=source_id, status="running", full_sync=full))
            session.commit()

        try:
            with self.db.session() as session:
                source = session.get(Source, source_id)
                previous = (source.cursor_at, source.cursor_id) if source.cursor_at else None
                rows = fetch_records(source.kind, source.config, self.settings, self.http_transport)
                run = session.get(SyncRun, token)
                run.fetched_count = len(rows)
                session.commit()
                # Invalid cursor rows are quarantined too; they cannot advance the watermark.
                eligible = [
                    r
                    for r in rows
                    if full
                    or previous is None
                    or record_cursor(r) is None
                    or record_cursor(r) > previous
                ]
                eligible.sort(
                    key=lambda r: record_cursor(r) or (parse_time("1970-01-01T00:00:00Z"), "")
                )

            seen = set()
            for offset in range(0, len(eligible), self.settings.batch_size):
                with self.db.session() as session:
                    source = session.get(Source, source_id)
                    if source.lease_owner != token or source.lease_expires_at < utcnow():
                        raise SourceBusy("Worker lost its source lease")
                    run = session.get(SyncRun, token)
                    for raw in eligible[offset : offset + self.settings.batch_size]:
                        run.read_count += 1
                        cursor = record_cursor(raw)
                        required = RECORD_TYPES[source.entity].model_fields
                        complete = all(
                            raw.get(key) is not None and str(raw[key]).strip() != ""
                            for key, field in required.items()
                            if field.is_required()
                        )
                        run.complete_count += int(complete)
                        if cursor is not None and cursor in seen:
                            self._issue(
                                session,
                                run,
                                raw,
                                "duplicate_in_batch",
                                "Repeated external_id and updated_at within one snapshot",
                            )
                            run.duplicate_count += 1
                        else:
                            if cursor is not None:
                                seen.add(cursor)
                            # Flush run counters outside the savepoint, so invalid rows still count.
                            session.flush()
                            checked_before, resolved_before = (
                                run.relation_checked,
                                run.relation_resolved,
                            )
                            try:
                                with session.begin_nested():
                                    record = RECORD_TYPES[source.entity].model_validate(raw)
                                    outcome = upsert_record(session, source, record, run)
                                    session.flush()
                                setattr(
                                    run, outcome + "_count", getattr(run, outcome + "_count") + 1
                                )
                            except (ValidationError, RecordError, IntegrityError) as exc:
                                # Savepoint rollback restores entity writes and ORM counters.
                                if isinstance(exc, RecordError) and exc.code == "unmapped_device":
                                    run.relation_checked = checked_before + 1
                                    run.relation_resolved = resolved_before
                                code = (
                                    exc.code
                                    if isinstance(exc, RecordError)
                                    else "schema_validation"
                                    if isinstance(exc, ValidationError)
                                    else "constraint_violation"
                                )
                                if isinstance(exc, ValidationError):
                                    message = "; ".join(
                                        f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                        for e in exc.errors()
                                    )
                                elif isinstance(exc, IntegrityError):
                                    message = "Record violates a unique key or relation constraint"
                                else:
                                    message = str(exc)
                                self._issue(session, run, raw, code, message)
                                run.rejected_count += 1
                        # Accepted and quarantined rows advance together; rejected data stays auditable.
                        if cursor is not None and (
                            source.cursor_at is None
                            or cursor > (source.cursor_at, source.cursor_id)
                        ):
                            source.cursor_at, source.cursor_id = cursor
                    source.lease_expires_at = utcnow() + timedelta(
                        seconds=self.settings.lease_seconds
                    )
                    session.commit()  # Data, errors, counters and watermark commit atomically.
                if (
                    fail_after_batches is not None
                    and (offset // self.settings.batch_size) + 1 >= fail_after_batches
                ):
                    raise InjectedFailure("Injected worker interruption after committed batch")

            self._finish(token, "succeeded", start)
        except Exception as exc:
            self._finish(token, "failed", start, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            with self.db.session() as session:
                session.execute(
                    update(Source)
                    .where(Source.id == source_id, Source.lease_owner == token)
                    .values(lease_owner=None, lease_expires_at=None)
                )
                session.commit()
        return token

    def _finish(self, token, status, start, error=None):
        with self.db.session() as session:
            run = session.get(SyncRun, token)
            if run.status != "running":
                return  # A new lease holder may already have marked this worker abandoned.
            run.status, run.finished_at, run.error = status, utcnow(), error
            run.duration_ms = round((time.perf_counter() - start) * 1000, 3)
            session.commit()

    @staticmethod
    def _issue(session, run, raw, code, message):
        session.add(
            QualityIssue(
                run_id=run.id,
                source_id=run.source_id,
                external_id=str(raw.get("external_id", ""))[:128] or None,
                code=code,
                message=message[:4000],
                raw=json.loads(json.dumps(raw, default=str)),
            )
        )
