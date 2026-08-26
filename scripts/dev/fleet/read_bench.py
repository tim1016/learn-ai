"""Read-storm benchmark: concurrent catalog + panel reads.

Usage:
    python3 read_bench.py --panel-sids a,b,c [--threads 6] [--iterations 10]
        [--results read_bench.jsonl]

Measures latency percentiles and — the WP2 (#1786) live gate — revision
drift: pure reads must not bump the panel revision (pre-fix every read
bumped it by +2, S16).

Read purity is only *claimed* when a revision-bearing panel was actually
sampled. With no ``--panel-sids`` the catalog storm runs alone, no revision
is ever observed, and an empty drift set means "not measured" — reporting
that as "pure reads hold" would let a regression where catalog GETs bump the
revision sail through the benchmark untouched.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _api

logger = logging.getLogger("read_bench")


def run(threads: int, iterations: int, panel_sids: list[str], results_path: str) -> int:
    latencies: dict[str, list[float]] = {"catalog": [], "panel": []}
    revisions: dict[str, list[int]] = {}
    errors: list[str] = []
    lock = threading.Lock()

    def worker(worker_id: int) -> None:
        for i in range(iterations):
            try:
                status, payload, latency = _api.request(
                    "GET",
                    f"/api/brokers/{_api.BROKER}/accounts/{_api.ACCOUNT_ID}/bots/catalog",
                )
                with lock:
                    latencies["catalog"].append(latency)
                    if status != 200:
                        errors.append(f"catalog {status}")
            except Exception as exc:  # the bench must count failures, not die on them
                with lock:
                    errors.append(f"catalog EXC {exc}")
            sid = panel_sids[(worker_id + i) % len(panel_sids)] if panel_sids else None
            if sid:
                try:
                    status, payload, latency = _api.request(
                        "GET",
                        f"/api/brokers/{_api.BROKER}/accounts/{_api.ACCOUNT_ID}/bots/{sid}/panel",
                    )
                    with lock:
                        latencies["panel"].append(latency)
                        if status == 200:
                            revisions.setdefault(sid, []).append(payload["revision"])
                        else:
                            errors.append(f"panel {sid} {status}")
                except Exception as exc:  # the bench must count failures, not die on them
                    with lock:
                        errors.append(f"panel {sid} EXC {exc}")

    started = time.monotonic()
    pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    wall = time.monotonic() - started

    summary: dict[str, object] = {"wall_s": round(wall, 1), "errors": errors[:20], "error_count": len(errors)}
    for kind, values in latencies.items():
        if values:
            values.sort()
            summary[kind] = {
                "n": len(values),
                "p50_s": round(statistics.median(values), 2),
                "p95_s": round(values[int(0.95 * (len(values) - 1))], 2),
                "max_s": round(values[-1], 2),
            }
    drift = {
        sid: sorted(set(revs))
        for sid, revs in revisions.items()
        if len(set(revs)) > 1
    }
    sampled = sum(len(revs) for revs in revisions.values())
    summary["revision_samples"] = sampled
    if not sampled:
        summary["revision_drift"] = "NOT SAMPLED (no panel revision read; purity unproven)"
    else:
        summary["revision_drift"] = drift or "NONE (pure reads hold)"
    _api.emit(summary)
    _api.jsonl_append(results_path, {"kind": "read_bench_summary", **summary})
    return 1 if drift or errors or not sampled else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--panel-sids", default="")
    parser.add_argument("--results", default="read_bench.jsonl")
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be at least 1")
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")
    sids = [s for s in args.panel_sids.split(",") if s]
    if not sids:
        parser.error(
            "--panel-sids requires at least one bot: the revision-purity gate "
            "cannot be evaluated without a revision-bearing panel"
        )
    _api.setup_logging()
    return run(args.threads, args.iterations, sids, args.results)


if __name__ == "__main__":
    raise SystemExit(main())
