"""Lifecycle churn: stop -> verify terminal -> resume, over rotating cohorts.

Usage:
    python3 churn_wave.py --sids a,b,c,d --cohort 4 [--waves 3]
        [--results churn_wave.jsonl]

The wave the 2026-08-25 run aborted (S15c) before it could execute. Hunts:
fence races, double-resume, idempotency gaps, resume-admission regressions
under repeated cycling.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _api

logger = logging.getLogger("churn_wave")


def wait_running_state(sids: list[str], running: bool, timeout_s: float = 180.0) -> list[str]:
    """Poll the roster until every sid reaches the wanted running state."""
    deadline = time.monotonic() + timeout_s
    pending = set(sids)
    while pending and time.monotonic() < deadline:
        state = {
            b["strategy_instance_id"]: bool(b.get("running")) for b in _api.list_roster()
        }
        pending = {s for s in pending if state.get(s) != running}
        if pending:
            time.sleep(5.0)
    return sorted(pending)


def churn(sids: list[str], cohort_size: int, waves: int, results_path: str) -> int:
    failures = 0
    for wave in range(1, waves + 1):
        for start in range(0, len(sids), cohort_size):
            cohort = sids[start : start + cohort_size]
            logger.info("wave %d cohort %s: stopping", wave, cohort)
            for sid in cohort:
                try:
                    status, payload, latency = _api.post_action(
                        sid, "stop_bot_decisions", reason=f"churn wave {wave}"
                    )
                except RuntimeError as exc:
                    status, payload, latency = 0, {"detail": str(exc)}, 0.0
                _api.jsonl_append(
                    results_path,
                    {"wave": wave, "sid": sid, "op": "stop", "status": status,
                     "latency_s": round(latency, 1),
                     "detail": payload.get("detail") if status not in (200, 201) else None},
                )
                if status not in (200, 201):
                    failures += 1
            stuck = wait_running_state(cohort, running=False)
            if stuck:
                logger.error("wave %d: not terminal after stop: %s", wave, stuck)
                failures += len(stuck)
            logger.info("wave %d cohort %s: resuming", wave, cohort)
            for sid in cohort:
                try:
                    status, payload, latency = _api.post_action(
                        sid, "resume", reason=f"churn wave {wave}"
                    )
                except RuntimeError as exc:
                    status, payload, latency = 0, {"detail": str(exc)}, 0.0
                _api.jsonl_append(
                    results_path,
                    {"wave": wave, "sid": sid, "op": "resume", "status": status,
                     "latency_s": round(latency, 1),
                     "detail": payload.get("detail") if status not in (200, 201) else None},
                )
                if status not in (200, 201):
                    failures += 1
            stuck = wait_running_state(cohort, running=True)
            if stuck:
                logger.error("wave %d: not running after resume: %s", wave, stuck)
                failures += len(stuck)
    logger.info("churn complete: %d failures", failures)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sids", required=True)
    parser.add_argument("--cohort", type=int, default=4)
    parser.add_argument("--waves", type=int, default=1)
    parser.add_argument("--results", default="churn_wave.jsonl")
    args = parser.parse_args()
    _api.setup_logging()
    return churn([s for s in args.sids.split(",") if s], args.cohort, args.waves, args.results)


if __name__ == "__main__":
    raise SystemExit(main())
