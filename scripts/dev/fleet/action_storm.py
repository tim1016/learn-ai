"""Concurrent panel-action storm probes.

Usage:
    python3 action_storm.py replay <sid> <action_id> [--n 4]
    python3 action_storm.py conflict <sid> <action_a> <action_b>
    python3 action_storm.py fanout <action_id> --sids a,b,c [--reason TEXT]

Probes:
- replay:   N concurrent POSTs, SAME idempotency key -> exactly one
            applied=true expected (durable ledger under true concurrency,
            not just sequential replay).
- conflict: two DIFFERENT actions on one bot at the same instant -> the
            fence/executor must serialize or refuse coherently; no torn state.
- fanout:   the same action across many bots simultaneously (mass-stop
            latency under parallelism vs yesterday's sequential 50-100s).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _api

logger = logging.getLogger("action_storm")


def _collect(results: list, sid: str, action_id: str, idem: str, reason: str | None) -> None:
    try:
        panel = _api.get_panel(sid)
        status, payload, latency = _api.post_action(
            sid, action_id, reason=reason, idempotency_key=idem, panel=panel
        )
    except RuntimeError as exc:
        status, payload, latency = 0, {"detail": str(exc)}, 0.0
    results.append(
        {"sid": sid, "action_id": action_id, "idem": idem[:16], "status": status,
         "latency_s": round(latency, 1),
         "applied": payload.get("applied") if isinstance(payload, dict) else None,
         "outcome": payload.get("outcome") if isinstance(payload, dict) else None,
         "detail": str(payload.get("detail"))[:200] if isinstance(payload, dict) and status not in (200, 201) else None}
    )


def _run_threads(specs: list[tuple[str, str, str, str | None]]) -> list[dict]:
    results: list[dict] = []
    threads = [
        threading.Thread(target=_collect, args=(results, sid, action, idem, reason))
        for sid, action, idem, reason in specs
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for r in results:
        print(json.dumps(r))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["replay", "conflict", "fanout"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--n", type=int, default=4)
    parser.add_argument("--sids", default="")
    parser.add_argument("--reason", default="action storm probe")
    args = parser.parse_args()
    _api.setup_logging()

    if args.mode == "replay":
        sid, action_id = args.args[0], args.args[1]
        idem = f"storm-{uuid.uuid4().hex}"
        results = _run_threads([(sid, action_id, idem, args.reason)] * args.n)
        applied = [r for r in results if r.get("applied")]
        print(f"VERDICT: {len(applied)} applied of {len(results)} (expected exactly 1)")
        return 0 if len(applied) == 1 else 1
    if args.mode == "conflict":
        sid, action_a, action_b = args.args[0], args.args[1], args.args[2]
        results = _run_threads([
            (sid, action_a, f"storm-{uuid.uuid4().hex}", args.reason),
            (sid, action_b, f"storm-{uuid.uuid4().hex}", args.reason),
        ])
        ok = [r for r in results if r["status"] in (200, 201)]
        print(f"VERDICT: {len(ok)}/2 succeeded — inspect coherence above")
        return 0
    sids = [s for s in args.sids.split(",") if s]
    action_id = args.args[0]
    results = _run_threads(
        [(sid, action_id, f"storm-{uuid.uuid4().hex}", args.reason) for sid in sids]
    )
    ok = [r for r in results if r["status"] in (200, 201)]
    print(f"VERDICT: {len(ok)}/{len(sids)} succeeded concurrently")
    return 0 if len(ok) == len(sids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
