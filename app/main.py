import json
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.adapters import AdapterError, validate_config
from app.config import ROOT, Settings
from app.db import Database
from app.demo import seed_demo
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
from app.schemas import SourceCreate, SyncRequest, iso
from app.sync import SourceBusy, SyncService


def percentile(values, fraction):
    return (
        round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 3) if values else None
    )


def quality_counts(runs):
    total = sum(r.read_count for r in runs)
    checked = sum(r.relation_checked for r in runs)
    return {
        "processed_records": total,
        "required_field_completeness_pct": round(
            sum(r.complete_count for r in runs) / total * 100, 2
        )
        if total
        else None,
        "batch_uniqueness_pct": round(
            (total - sum(r.duplicate_count for r in runs)) / total * 100, 2
        )
        if total
        else None,
        "accepted_pct": round(
            sum(r.inserted_count + r.updated_count + r.unchanged_count for r in runs) / total * 100,
            2,
        )
        if total
        else None,
        "association_success_pct": round(sum(r.relation_resolved for r in runs) / checked * 100, 2)
        if checked
        else None,
        "association_attempts": checked,
    }


def run_dict(run, source_code=None):
    return {
        "id": run.id,
        "source_id": run.source_id,
        "source_code": source_code,
        "status": run.status,
        "full_sync": run.full_sync,
        **{
            key: getattr(run, key)
            for key in (
                "fetched_count",
                "read_count",
                "inserted_count",
                "updated_count",
                "unchanged_count",
                "rejected_count",
                "duplicate_count",
                "duration_ms",
                "error",
            )
        },
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "quality": quality_counts([run]),
    }


def create_app(settings: Settings | None = None, http_transport=None):
    settings = settings or Settings.from_env()
    db = Database(settings.database_url)
    service = SyncService(db, settings, http_transport)
    samples = defaultdict(lambda: deque(maxlen=1000))
    metrics_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_):
        db.initialize()
        if settings.demo_mode:
            seed_demo(db, settings)
        yield
        db.close()

    api = FastAPI(
        title="工业运维数据中台",
        version="0.1.0",
        lifespan=lifespan,
        description="CSV / HTTP / SQL ingestion, device identity, incremental sync and data quality. Local demo; write APIs have no authentication.",
    )
    api.state.db, api.state.service, api.state.settings = db, service, settings

    def session_dependency():
        with db.session() as session:
            yield session

    @api.middleware("http")
    async def trace_requests(request: Request, call_next):
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["Server-Timing"] = f"app;dur={elapsed:.3f}"
        route = getattr(request.scope.get("route"), "path", None)
        if route and route.startswith("/api/") and route != "/api/metrics":
            with metrics_lock:
                samples[f"{request.method} {route}"].append(elapsed)
        return response

    @api.get("/health")
    def health(session=Depends(session_dependency)):
        session.execute(select(1))
        return {"status": "ok", "demo_mode": settings.demo_mode}

    @api.get("/api/sources")
    def sources(session=Depends(session_dependency)):
        rows = session.scalars(select(Source).order_by(Source.id)).all()
        output = []
        for source in rows:
            last = session.scalar(
                select(SyncRun)
                .where(SyncRun.source_id == source.id)
                .order_by(SyncRun.started_at.desc())
                .limit(1)
            )
            output.append(
                {
                    "id": source.id,
                    "code": source.code,
                    "name": source.name,
                    "kind": source.kind,
                    "entity": source.entity,
                    "config": source.config,
                    "cursor": {
                        "updated_at": iso(source.cursor_at),
                        "external_id": source.cursor_id,
                    },
                    "syncing": bool(
                        source.lease_owner
                        and source.lease_expires_at
                        and source.lease_expires_at > utcnow()
                    ),
                    "last_run": run_dict(last) if last else None,
                }
            )
        return {"items": output}

    @api.post("/api/sources", status_code=201)
    def add_source(body: SourceCreate, session=Depends(session_dependency)):
        try:
            validate_config(body.kind, body.config, settings)
        except AdapterError as exc:
            raise HTTPException(422, str(exc)) from exc
        source = Source(**body.model_dump())
        session.add(source)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(409, "Source code already exists") from exc
        return {"id": source.id, "code": source.code}

    @api.post("/api/sources/{source_id}/sync")
    def sync(
        source_id: int, body: SyncRequest = SyncRequest(), session=Depends(session_dependency)
    ):
        try:
            run_id = service.sync(source_id, full=body.full)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SourceBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                502, "Source sync failed; inspect /api/runs for the error and committed progress"
            ) from exc
        return run_dict(session.get(SyncRun, run_id))

    @api.get("/api/runs")
    def runs(limit: int = Query(50, ge=1, le=200), session=Depends(session_dependency)):
        rows = session.execute(
            select(SyncRun, Source.code)
            .join(Source)
            .order_by(SyncRun.started_at.desc())
            .limit(limit)
        ).all()
        return {"items": [run_dict(run, code) for run, code in rows]}

    @api.get("/api/runs/{run_id}")
    def run_detail(run_id: str, session=Depends(session_dependency)):
        run = session.get(SyncRun, run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        return run_dict(run, session.get(Source, run.source_id).code)

    @api.get("/api/quality")
    def quality(
        run_id: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        session=Depends(session_dependency),
    ):
        query = select(QualityIssue, Source.code).join(Source)
        if run_id:
            query = query.where(QualityIssue.run_id == run_id)
        issues = session.execute(query.order_by(QualityIssue.id.desc()).limit(limit)).all()
        recent = session.scalars(
            select(SyncRun).order_by(SyncRun.started_at.desc()).limit(100)
        ).all()
        return {
            "scope": "last_100_runs_processed_records",
            "summary": quality_counts(recent),
            "issues": [
                {
                    "id": i.id,
                    "run_id": i.run_id,
                    "source_code": code,
                    "external_id": i.external_id,
                    "code": i.code,
                    "message": i.message,
                    "raw": i.raw,
                    "created_at": iso(i.created_at),
                }
                for i, code in issues
            ],
        }

    @api.get("/api/devices")
    def devices(
        q: str = Query("", max_length=128),
        limit: int = Query(100, ge=1, le=200),
        session=Depends(session_dependency),
    ):
        query = select(Device)
        if q:
            query = query.where(
                or_(
                    Device.device_key.contains(q, autoescape=True),
                    Device.name.contains(q, autoescape=True),
                )
            )
        rows = session.scalars(query.order_by(Device.device_key).limit(limit)).all()
        return {
            "items": [
                {
                    "device_key": d.device_key,
                    "name": d.name,
                    "category": d.category,
                    "location": d.location,
                    "status": d.status,
                    "source_updated_at": iso(d.source_updated_at),
                }
                for d in rows
            ]
        }

    def require_device(key, session):
        device = session.scalar(select(Device).where(Device.device_key == key))
        if not device:
            raise HTTPException(404, "Device not found")
        return device

    @api.get("/api/devices/{device_key}")
    def device_detail(device_key: str, session=Depends(session_dependency)):
        d = require_device(device_key, session)
        aliases = session.execute(
            select(DeviceAlias, Source.code).join(Source).where(DeviceAlias.device_id == d.id)
        ).all()
        return {
            "device_key": d.device_key,
            "name": d.name,
            "category": d.category,
            "location": d.location,
            "status": d.status,
            "source_updated_at": iso(d.source_updated_at),
            "ingested_at": iso(d.ingested_at),
            "aliases": [
                {"source_code": code, "external_device_id": a.external_device_id}
                for a, code in aliases
            ],
            "alarm_count": session.scalar(
                select(func.count()).select_from(Alarm).where(Alarm.device_id == d.id)
            ),
            "maintenance_count": session.scalar(
                select(func.count()).select_from(Maintenance).where(Maintenance.device_id == d.id)
            ),
        }

    @api.get("/api/devices/{device_key}/timeline")
    def timeline(
        device_key: str, limit: int = Query(50, ge=1, le=200), session=Depends(session_dependency)
    ):
        d = require_device(device_key, session)
        alarms = session.scalars(
            select(Alarm)
            .where(Alarm.device_id == d.id)
            .order_by(Alarm.occurred_at.desc(), Alarm.id.desc())
            .limit(limit)
        ).all()
        repairs = session.scalars(
            select(Maintenance)
            .where(Maintenance.device_id == d.id)
            .order_by(Maintenance.performed_at.desc(), Maintenance.id.desc())
            .limit(limit)
        ).all()
        codes = dict(session.execute(select(Source.id, Source.code)).all())
        items = [
            dict(
                type="alarm",
                external_id=a.external_id,
                at=iso(a.occurred_at),
                title=a.code,
                description=a.message,
                severity=a.severity,
                source_code=codes[a.source_id],
                source_updated_at=iso(a.source_updated_at),
            )
            for a in alarms
        ]
        items += [
            dict(
                type="maintenance",
                external_id=r.external_id,
                at=iso(r.performed_at),
                title="维修记录",
                description=r.description,
                technician=r.technician,
                cost=str(r.cost),
                source_code=codes[r.source_id],
                source_updated_at=iso(r.source_updated_at),
            )
            for r in repairs
        ]
        items.sort(key=lambda x: (x["at"], x["type"], x["external_id"]), reverse=True)
        return {"device_key": d.device_key, "items": items[:limit]}

    @api.get("/api/overview")
    def overview(session=Depends(session_dependency)):
        recent = session.scalars(
            select(SyncRun).order_by(SyncRun.started_at.desc()).limit(100)
        ).all()
        freshness = []
        for source in session.scalars(select(Source).order_by(Source.id)).all():
            model = {"device": Device, "alarm": Alarm, "maintenance": Maintenance}[source.entity]
            newest = session.scalar(
                select(func.max(model.source_updated_at)).where(model.source_id == source.id)
            )
            freshness.append(
                {
                    "source_code": source.code,
                    "latest_accepted_update": iso(newest),
                    "latest_record_age_seconds": round((utcnow() - newest).total_seconds(), 1)
                    if newest
                    else None,
                }
            )
        return {
            "counts": {
                name: session.scalar(select(func.count()).select_from(model))
                for name, model in (
                    ("devices", Device),
                    ("alarms", Alarm),
                    ("maintenance", Maintenance),
                    ("sources", Source),
                    ("quality_issues", QualityIssue),
                )
            },
            "quality": quality_counts(recent),
            "freshness": freshness,
            "sync_p95_ms": percentile(
                [r.duration_ms for r in recent if r.duration_ms is not None], 0.95
            ),
            "demo_mode": settings.demo_mode,
        }

    @api.get("/api/metrics")
    def metrics():
        with metrics_lock:
            data = {path: list(values) for path, values in samples.items()}
        return {
            "scope": "in_process_last_1000_requests_per_route",
            "includes": "server-side handler latency, excluding network transfer",
            "routes": [
                {
                    "route": route,
                    "samples": len(values),
                    "p50_ms": percentile(values, 0.5),
                    "p95_ms": percentile(values, 0.95),
                }
                for route, values in sorted(data.items())
            ],
        }

    @api.post("/api/demo/run")
    def demo_run(session=Depends(session_dependency)):
        if not settings.demo_mode:
            raise HTTPException(404, "Demo mode disabled")
        output = []
        for code in ("asset-registry", "alarm-feed", "maintenance-db"):
            source = session.scalar(select(Source).where(Source.code == code))
            try:
                run_id = service.sync(source.id)
                session.rollback()  # End any MySQL repeatable-read snapshot before reading the new run.
                output.append(run_dict(session.get(SyncRun, run_id), code))
            except SourceBusy as exc:
                raise HTTPException(409, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(502, f"Sync failed for {code}; inspect /api/runs") from exc
        return {"items": output}

    @api.get("/demo-source/alarms", include_in_schema=False)
    def demo_alarm_source():
        if not settings.demo_mode:
            raise HTTPException(404, "Demo mode disabled")
        return json.loads((settings.import_root / "alarms.json").read_text(encoding="utf-8"))

    @api.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(ROOT / "app/static/index.html")

    api.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")
    return api


app = create_app()
