import csv
from datetime import timedelta
from threading import Event, Thread

import httpx
import pytest
from sqlalchemy import func, select

from app import sync as sync_module
from app.main import percentile
from app.models import Device, Source, SyncRun, utcnow
from app.schemas import iso
from app.sync import InjectedFailure, SourceBusy


def sources(env):
    return {s["code"]: s["id"] for s in env["client"].get("/api/sources").json()["items"]}


def read_devices(env):
    with (env["settings"].import_root / "devices.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_devices(env, rows):
    with (env["settings"].import_root / "devices.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_three_adapters_join_device_identity_and_timeline(env):
    client = env["client"]
    response = client.post("/api/demo/run")
    assert response.status_code == 200, response.text
    runs = response.json()["items"]
    assert sum(r["inserted_count"] for r in runs) == 8
    assert sum(r["rejected_count"] for r in runs) == 4
    assert sum(r["duplicate_count"] for r in runs) == 1
    assert all(
        r["read_count"]
        == sum(
            r[k]
            for k in (
                "inserted_count",
                "updated_count",
                "unchanged_count",
                "rejected_count",
                "duplicate_count",
            )
        )
        for r in runs
    )
    summary = client.get("/api/overview").json()
    assert summary["counts"] == {
        "devices": 3,
        "alarms": 3,
        "maintenance": 2,
        "sources": 3,
        "quality_issues": 5,
    }
    detail = client.get("/api/devices/EQ-001").json()
    assert {a["external_device_id"] for a in detail["aliases"]} == {"EQ-001", "PLC-A01", "M-1001"}
    assert detail["alarm_count"] == 2 and detail["maintenance_count"] == 1
    events = client.get("/api/devices/EQ-001/timeline").json()["items"]
    assert [e["external_id"] for e in events] == ["A-003", "A-001", "R-001"]
    assert events[1]["severity"] == "critical"
    assert events[-1]["cost"] == "450.00"


def test_incremental_repeat_reads_nothing_and_full_replay_is_idempotent(env):
    client = env["client"]
    client.post("/api/demo/run").raise_for_status()
    counts = client.get("/api/overview").json()["counts"]
    repeat = client.post("/api/demo/run").json()["items"]
    assert all(r["read_count"] == 0 for r in repeat)
    full = [
        client.post(f"/api/sources/{id}/sync", json={"full": True}).json()
        for id in sources(env).values()
    ]
    assert sum(r["unchanged_count"] for r in full) == 8
    assert sum(r["inserted_count"] for r in full) == 0
    new_counts = client.get("/api/overview").json()["counts"]
    assert {k: v for k, v in counts.items() if k != "quality_issues"} == {
        k: v for k, v in new_counts.items() if k != "quality_issues"
    }


def test_newer_version_updates_existing_device(env):
    client = env["client"]
    source_id = sources(env)["asset-registry"]
    env["service"].sync(source_id)
    rows = read_devices(env)
    newer = {**rows[0], "name": "已更新的空压机", "updated_at": iso(utcnow())}
    write_devices(env, rows + [newer])
    result = client.post(f"/api/sources/{source_id}/sync").json()
    assert result["updated_count"] == 1 and result["inserted_count"] == 0
    assert client.get("/api/devices/EQ-001").json()["name"] == "已更新的空压机"


def test_unchanged_timestamp_with_changed_payload_is_quarantined(env):
    source_id = sources(env)["asset-registry"]
    env["service"].sync(source_id)
    rows = read_devices(env)
    rows[0]["name"] = "incorrect unversioned update"
    # Keep only the conflicting row so the test addresses persisted version checks.
    write_devices(env, [rows[0]])
    env["service"].sync(source_id, full=True)
    issues = env["client"].get("/api/quality").json()["issues"]
    assert issues[0]["code"] == "version_conflict"
    assert env["client"].get("/api/devices/EQ-001").json()["name"] == "一号空压机"


def test_committed_batch_survives_interruption_and_resume_does_not_lose_rows(env):
    source_id = sources(env)["asset-registry"]
    with pytest.raises(InjectedFailure):
        env["service"].sync(source_id, fail_after_batches=1)
    with env["db"].session() as session:
        assert session.scalar(select(func.count()).select_from(Device)) == 1
        source = session.get(Source, source_id)
        assert source.cursor_id == "EQ-001"
        assert source.lease_owner is None
    env["service"].sync(source_id)
    assert len(env["client"].get("/api/devices").json()["items"]) == 3
    statuses = {r["status"] for r in env["client"].get("/api/runs").json()["items"]}
    assert statuses == {"failed", "succeeded"}


def test_uncommitted_batch_and_cursor_roll_back_together(env, monkeypatch):
    rows = read_devices(env)[:3]
    write_devices(env, rows)
    real = sync_module.upsert_record
    calls = 0

    def fail_during_batch(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("worker failed before batch commit")
        return real(*args)

    monkeypatch.setattr(sync_module, "upsert_record", fail_during_batch)
    source_id = sources(env)["asset-registry"]
    with pytest.raises(RuntimeError):
        env["service"].sync(source_id)
    with env["db"].session() as session:
        assert session.scalar(select(func.count()).select_from(Device)) == 0
        assert session.get(Source, source_id).cursor_at is None
        run = session.scalar(select(SyncRun))
        assert run.read_count == 0
    monkeypatch.setattr(sync_module, "upsert_record", real)
    env["service"].sync(source_id)
    assert len(env["client"].get("/api/devices").json()["items"]) == 3


def test_mapping_conflict_rolls_back_entire_device_record(env):
    source_id = sources(env)["asset-registry"]
    env["service"].sync(source_id)
    row = {**read_devices(env)[0], "external_id": "EQ-999", "updated_at": iso(utcnow())}
    write_devices(env, [row])
    env["service"].sync(source_id)
    assert env["client"].get("/api/devices/EQ-999").status_code == 404
    assert env["client"].get("/api/quality").json()["issues"][0]["code"] == "mapping_conflict"
    assert env["client"].get("/api/devices/EQ-001").json()["alarm_count"] == 0


def test_quality_metrics_have_explicit_denominators(env):
    env["client"].post("/api/demo/run").raise_for_status()
    report = env["client"].get("/api/quality").json()
    assert report["summary"]["processed_records"] == 13
    assert report["summary"]["required_field_completeness_pct"] == 92.31
    assert report["summary"]["batch_uniqueness_pct"] == 92.31
    assert report["summary"]["association_attempts"] == 6
    assert report["summary"]["association_success_pct"] == 83.33
    assert {i["code"] for i in report["issues"]} == {
        "schema_validation",
        "unmapped_device",
        "duplicate_in_batch",
    }


def test_timezone_normalization_and_reject_ambiguous_naive_time(env):
    row = read_devices(env)[0]
    row["updated_at"] = "2026-11-25T16:00:00+08:00"
    write_devices(env, [row])
    source_id = sources(env)["asset-registry"]
    env["service"].sync(source_id)
    assert (
        env["client"].get("/api/devices/EQ-001").json()["source_updated_at"]
        == "2026-11-25T08:00:00.000Z"
    )
    row["external_id"] = "EQ-NAIVE"
    row["updated_at"] = "2026-11-26T08:00:00"
    row["aliases"] = "{}"
    write_devices(env, [row])
    env["service"].sync(source_id)
    assert env["client"].get("/api/devices/EQ-NAIVE").status_code == 404


def test_expired_worker_lease_is_reclaimed_and_old_run_marked_abandoned(env):
    source_id = sources(env)["asset-registry"]
    with env["db"].session() as session:
        source = session.get(Source, source_id)
        source.lease_owner = "old-worker"
        source.lease_expires_at = utcnow() - timedelta(seconds=5)
        session.add(SyncRun(id="old-worker", source_id=source_id, status="running"))
        session.commit()
    env["service"].sync(source_id)
    assert env["client"].get("/api/runs/old-worker").json()["status"] == "abandoned"


def test_concurrent_sync_cannot_acquire_same_source_lease(env, monkeypatch):
    source_id = sources(env)["asset-registry"]
    entered, release = Event(), Event()
    real = sync_module.fetch_records
    failures = []

    def slow_fetch(*args):
        entered.set()
        assert release.wait(timeout=10)
        return real(*args)

    def worker():
        try:
            env["service"].sync(source_id)
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(sync_module, "fetch_records", slow_fetch)
    thread = Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(timeout=10)
        with pytest.raises(SourceBusy):
            env["service"].sync(source_id)
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and not failures


def test_http_failure_is_recorded_without_advancing_cursor(env):
    source_id = sources(env)["alarm-feed"]
    original = env["service"].http_transport
    env["service"].http_transport = httpx.MockTransport(lambda _: httpx.Response(503))
    response = env["client"].post(f"/api/sources/{source_id}/sync")
    assert response.status_code == 502
    with env["db"].session() as session:
        assert session.get(Source, source_id).cursor_at is None
        assert session.get(Source, source_id).lease_owner is None
    assert env["client"].get("/api/runs").json()["items"][0]["status"] == "failed"
    env["service"].http_transport = original


@pytest.mark.parametrize(
    "kind,config",
    [
        ("csv", {"path": "../outside.csv"}),
        ("http", {"url": "http://example.com/records"}),
        ("http", {"url": "http://name:password@source.test/records"}),
        ("http", {"url": "http://[bad-ipv6/records"}),
        ("http", {"url": "http://source.test:999999/records"}),
        ("sqlite", {"path": "maintenance.db", "table": "a; DROP TABLE devices"}),
    ],
)
def test_source_configuration_rejects_unbounded_access(env, kind, config):
    response = env["client"].post(
        "/api/sources",
        json={
            "code": "bad-source",
            "name": "bad",
            "kind": kind,
            "entity": "device",
            "config": config,
        },
    )
    assert response.status_code == 422


def test_api_validation_search_and_actual_latency_samples(env):
    client = env["client"]
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/api/devices?limit=10000").status_code == 422
    assert client.get("/api/devices/missing/timeline").status_code == 404
    client.post("/api/demo/run").raise_for_status()
    assert len(client.get("/api/devices?q=空压机").json()["items"]) == 1
    assert client.get("/api/devices?q=%").json()["items"] == []
    metrics = client.get("/api/metrics").json()
    assert any(
        m["route"] == "GET /api/devices" and m["samples"] >= 2 and m["p95_ms"] > 0
        for m in metrics["routes"]
    )
    assert percentile(list(range(1, 101)), 0.95) == 95
    assert percentile([], 0.95) is None


def test_compound_watermark_does_not_skip_distinct_ids_with_equal_timestamp(env):
    rows = read_devices(env)[:2]
    rows[1]["updated_at"] = rows[0]["updated_at"]
    source_id = sources(env)["asset-registry"]
    write_devices(env, [rows[0]])
    env["service"].sync(source_id)
    write_devices(env, rows)
    run_id = env["service"].sync(source_id)
    result = env["client"].get(f"/api/runs/{run_id}").json()
    assert result["read_count"] == 1 and result["inserted_count"] == 1
    assert len(env["client"].get("/api/devices").json()["items"]) == 2


def test_late_record_requires_full_replay_and_does_not_regress_watermark(env):
    source_id = sources(env)["asset-registry"]
    env["service"].sync(source_id)
    with env["db"].session() as session:
        source = session.get(Source, source_id)
        frontier = (source.cursor_at, source.cursor_id)
    row = {**read_devices(env)[0], "external_id": "EQ-LATE", "aliases": "{}"}
    write_devices(env, [row])
    env["service"].sync(source_id)
    assert env["client"].get("/api/devices/EQ-LATE").status_code == 404
    env["service"].sync(source_id, full=True)
    assert env["client"].get("/api/devices/EQ-LATE").status_code == 200
    with env["db"].session() as session:
        source = session.get(Source, source_id)
        assert (source.cursor_at, source.cursor_id) == frontier
