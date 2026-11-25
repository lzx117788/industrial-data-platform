"""Measure real local HTTP calls; output includes workload and environment."""

import argparse
import json
import platform
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.config import ROOT
from app.main import percentile
from app.models import utcnow
from app.schemas import iso


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.requests <= 10000 or not 1 <= args.concurrency <= 50:
        parser.error("requests must be 1..10000 and concurrency 1..50")
    with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as client:
        counts = client.get("/api/overview").raise_for_status().json()["counts"]
        client.get("/api/devices/EQ-001").raise_for_status()

        def request(_):
            started = time.perf_counter()
            response = client.get("/api/devices/EQ-001")
            response.raise_for_status()
            assert response.json()["device_key"] == "EQ-001"
            return (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            values = list(pool.map(request, range(args.requests)))
        total_seconds = time.perf_counter() - started
    report = {
        "measured_at": iso(utcnow()),
        "endpoint": "GET /api/devices/EQ-001",
        "base_url": args.base_url,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "dataset_counts": counts,
        "successful_requests": len(values),
        "latency_scope": "client-observed HTTP round trip, one warmup request excluded",
        "p50_ms": percentile(values, 0.5),
        "p95_ms": percentile(values, 0.95),
        "requests_per_second": round(args.requests / total_seconds, 2),
        "environment": {"os": platform.platform(), "python": platform.python_version()},
        "limitation": "Small synthetic local dataset; not production-scale throughput evidence.",
    }
    output = ROOT / "reports/benchmark.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
