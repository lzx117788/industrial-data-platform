import argparse
import json
import os
import time

from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.demo import seed_demo
from app.models import Source
from app.sync import SyncService


def main():
    parser = argparse.ArgumentParser(description="Industrial data platform CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("init", help="Create tables and optional demo fixtures")
    sync = commands.add_parser("sync-all", help="Sync all sources, devices first")
    sync.add_argument("--full", action="store_true")
    sync.add_argument(
        "--interval", type=int, default=0, help="Repeat interval in seconds; 0 means once"
    )
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        os.environ.setdefault("DEMO_BASE_URL", f"http://127.0.0.1:{args.port}")
        uvicorn.run("app.main:app", host=args.host, port=args.port)
        return
    settings = Settings.from_env()
    db = Database(settings.database_url)
    db.initialize()
    if settings.demo_mode:
        seed_demo(db, settings)
    if args.command == "init":
        print("Tables and demo sources are ready.")
        db.close()
        return
    if args.interval < 0:
        parser.error("--interval must be nonnegative")
    service = SyncService(db, settings)
    try:
        while True:
            with db.session() as session:
                sources = session.scalars(select(Source)).all()
                source_ids = [
                    (s.id, s.code)
                    for s in sorted(sources, key=lambda s: (s.entity != "device", s.id))
                ]
            for source_id, code in source_ids:
                try:
                    run_id = service.sync(source_id, full=args.full)
                    print(json.dumps({"source": code, "run_id": run_id, "status": "succeeded"}))
                except Exception as exc:
                    print(json.dumps({"source": code, "status": "failed", "error": str(exc)}))
                    if not args.interval:
                        raise
            if not args.interval:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()


if __name__ == "__main__":
    main()
