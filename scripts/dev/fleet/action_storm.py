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

Every panel snapshot is read BEFORE any thread starts. A worker that read
its own panel would be racing the other workers' mutations: late workers
find the action already disabled or absent, never POST, and a "1 applied of
N" verdict passes without the ledger ever seeing concurrency. Binding one
pre-thread snapshot is what makes these probes mean what they claim.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _api

logger = logging.getLogger("action_storm")

_SUCCESS_STATUSES = (200, 201)


def _collect(
    results: list[dict],
    lock: threading.Lock,
    sid: str,
    action_id: str,
    idem: str,
    reason: str | None,
    panel: dict | None,
) -> None:
    """Post one storm request and record its outcome, however it ends.

    A worker that raises without recording silently shrinks the sample, and
    a "1 applied" verdict over a shrunken sample is a false pass. Every
    failure mode -- an unpresented/disabled action (``RuntimeError``), a
    dropped socket, a timeout -- lands in ``results`` instead.
    """
    try:
        status, payload, latency = _api.post_action(
            sid, action_id, reason=reason, idempotency_key=idem, panel=panel
        )
    except Exception as exc:  # a dead worker must still report
        status = _api.TRANSPORT_FAILURE_STATUS
        payload = {"detail": f"{type(exc).__name__}: {exc}"}
        latency = 0.0
    record = {
        "sid": sid,
        "action_id": action_id,
        "idem": idem[:16],
        "status": status,
        "latency_s": round(latency, 1),
        "applied": payload.get("applied") if isinstance(payload, dict) else None,
        "outcome": payload.get("outcome") if isinstance(payload, dict) else None,
        "detail": (
            str(payload.get("detail"))[:200]
            if isinstance(payload, dict) and status not in _SUCCESS_STATUSES
            else None
        ),
    }
    with lock:
        results.append(record)


def _snapshot_panels(sids: list[str]) -> dict[str, dict | None]:
    """Read each bot's panel once, before any storm thread starts."""
    snapshots: dict[str, dict | None] = {}
    for sid in sids:
        if sid in snapshots:
            continue
        try:
            snapshots[sid] = _api.get_panel(sid)
        except Exception as exc:  # an unreadable panel is itself a result
            logger.error("panel snapshot failed for %s: %s", sid, exc)
            snapshots[sid] = None
    return snapshots


def _run_threads(specs: list[tuple[str, str, str, str | None]]) -> list[dict]:
    panels = _snapshot_panels([sid for sid, _action, _idem, _reason in specs])
    results: list[dict] = []
    lock = threading.Lock()
    threads = [
        threading.Thread(
            target=_collect,
            args=(results, lock, sid, action, idem, reason, panels.get(sid)),
        )
        for sid, action, idem, reason in specs
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for r in results:
        _api.emit(r)
    return results


def _responded(results: list[dict]) -> list[dict]:
    return [r for r in results if r["status"] != _api.TRANSPORT_FAILURE_STATUS]


def _succeeded(results: list[dict]) -> list[dict]:
    return [r for r in results if r["status"] in _SUCCESS_STATUSES]


def _validated_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    """Refuse a workload that cannot exercise the probe it claims to run."""
    expected_positionals = {"replay": 2, "conflict": 3, "fanout": 1}[args.mode]
    if len(args.args) != expected_positionals:
        parser.error(f"{args.mode} takes exactly {expected_positionals} positional argument(s)")
    if args.n < 1:
        parser.error("--n must be at least 1")
    if args.mode == "fanout" and not [s for s in args.sids.split(",") if s]:
        parser.error("fanout requires at least one --sids value")
    return args


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["replay", "conflict", "fanout"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--n", type=int, default=4)
    parser.add_argument("--sids", default="")
    parser.add_argument("--reason", default="action storm probe")
    args = _validated_args(parser, parser.parse_args())
    _api.setup_logging()

    if args.mode == "replay":
        sid, action_id = args.args[0], args.args[1]
        idem = f"storm-{uuid.uuid4().hex}"
        results = _run_threads([(sid, action_id, idem, args.reason)] * args.n)
        applied = [r for r in results if r.get("applied")]
        complete = len(results) == args.n
        _api.emit_verdict(
            f"VERDICT: {len(applied)} applied of {len(results)}/{args.n} requests "
            f"(expected exactly 1 applied over a complete sample)"
        )
        return 0 if complete and len(applied) == 1 else 1

    if args.mode == "conflict":
        sid, action_a, action_b = args.args[0], args.args[1], args.args[2]
        results = _run_threads(
            [
                (sid, action_a, f"storm-{uuid.uuid4().hex}", args.reason),
                (sid, action_b, f"storm-{uuid.uuid4().hex}", args.reason),
            ]
        )
        # Serialization is only *demonstrated* when both requests reached the
        # authority and at least one was applied. Two refusals, or a request
        # that never got a response, prove nothing about the fence -- exiting
        # 0 there would record an unrun probe as a passed one.
        responded = _responded(results)
        succeeded = _succeeded(results)
        coherent = len(results) == 2 and len(responded) == 2 and len(succeeded) >= 1
        _api.emit_verdict(
            f"VERDICT: {len(succeeded)}/2 applied, {len(responded)}/2 answered by the "
            f"authority — {'coherent serialization' if coherent else 'NOT DEMONSTRATED'}"
        )
        return 0 if coherent else 1

    sids = [s for s in args.sids.split(",") if s]
    action_id = args.args[0]
    results = _run_threads(
        [(sid, action_id, f"storm-{uuid.uuid4().hex}", args.reason) for sid in sids]
    )
    succeeded = _succeeded(results)
    _api.emit_verdict(f"VERDICT: {len(succeeded)}/{len(sids)} succeeded concurrently")
    return 0 if len(succeeded) == len(sids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
