"""Reproduce a real committed-batch interruption and recovery in an isolated database."""

import json
import tempfile
from pathlib import Path

from sqlalchemy import func, select

from app.config import ROOT, Settings
from app.db import Database
from app.demo import seed_demo
from app.models import Device, Source, SyncRun
from app.schemas import iso
from app.sync import InjectedFailure, SyncService


def main():
    with tempfile.TemporaryDirectory(prefix="ops-data-verify-") as temporary:
        folder = Path(temporary)
        settings = Settings(
            database_url=f"sqlite:///{folder / 'platform.db'}",
            import_root=folder / "imports",
            batch_size=2,
        )
        db = Database(settings.database_url)
        db.initialize()
        seed_demo(db, settings)
        service = SyncService(db, settings)
        with db.session() as session:
            source_id = session.scalar(select(Source.id).where(Source.code == "asset-registry"))
        interrupted = False
        try:
            service.sync(source_id, fail_after_batches=1)
        except InjectedFailure:
            interrupted = True
        with db.session() as session:
            before = session.scalar(select(func.count()).select_from(Device))
            source = session.get(Source, source_id)
            cursor = {"updated_at": iso(source.cursor_at), "external_id": source.cursor_id}
        resumed = service.sync(source_id)
        replay = service.sync(source_id, full=True)
        with db.session() as session:
            after = session.scalar(select(func.count()).select_from(Device))
            run = session.get(SyncRun, resumed)
            full = session.get(SyncRun, replay)
            report = {
                "scenario": "commit one batch, interrupt worker, resume from persisted watermark, full replay",
                "injected_interruption_observed": interrupted,
                "device_count_after_interruption": before,
                "persisted_cursor": cursor,
                "device_count_after_resume": after,
                "resume_inserted": run.inserted_count,
                "full_replay_inserted": full.inserted_count,
                "full_replay_unchanged": full.unchanged_count,
                "passed": interrupted
                and before == 1
                and after == 3
                and full.inserted_count == 0
                and full.unchanged_count == 3,
            }
        db.close()
    output = ROOT / "reports/recovery.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
