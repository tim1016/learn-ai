"""Mass-stop every running bot on the account (wind-down first tap).

Usage:
    python3 runner_stop.py [--only sid1,sid2] [--results runner_stop.jsonl]

Stops are posted sequentially with fresh token binds; failures are recorded
and retried once at the end. Flatten comes AFTER mass-stop settles (S13:
freshness budget vs action latency — never interleave).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _api

logger = logging.getLogger("runner_stop")


def stop_all(only: set[str] | None, results_path: str) -> int:
    running = [
        b["strategy_instance_id"]
        for b in _api.list_roster()
        if b.get("running") and (only is None or b["strategy_instance_id"] in only)
    ]
    logger.info("stopping %d running bots", len(running))
    failed: list[str] = []
    for round_no in (1, 2):
        targets = running if round_no == 1 else failed
        if round_no == 2 and targets:
            logger.info("retry round for %d failures", len(targets))
            failed = []
        for sid in targets:
            try:
                status, payload, latency = _api.post_action(
                    sid, "stop_bot_decisions", reason="fleet wind-down mass stop"
                )
            except RuntimeError as exc:
                status, payload, latency = 0, {"detail": str(exc)}, 0.0
            _api.jsonl_append(
                results_path,
                {
                    "round": round_no,
                    "sid": sid,
                    "status": status,
                    "latency_s": round(latency, 1),
                    "detail": payload.get("detail") if status not in (200, 201) else None,
                },
            )
            if status in (200, 201):
                logger.info("stopped %s (%.1fs)", sid, latency)
            else:
                logger.warning("stop %s -> %s", sid, status)
                failed.append(sid)
            time.sleep(1.0)
        if round_no == 1 and failed:
            time.sleep(20.0)
    if failed:
        logger.error("still failed after retry: %s", failed)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None)
    parser.add_argument("--results", default="runner_stop.jsonl")
    args = parser.parse_args()
    _api.setup_logging()
    only = set(args.only.split(",")) if args.only else None
    return stop_all(only, args.results)


if __name__ == "__main__":
    raise SystemExit(main())
