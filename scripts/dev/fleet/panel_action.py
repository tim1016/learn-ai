"""Token-rebinding panel-action driver.

Usage:
    python3 panel_action.py <sid> <action_id> [--reason TEXT] [--idem KEY]
        [--allow-disabled] [--repeat N]

Reads the panel, binds the action's own revision + concurrency_token, POSTs.
``--repeat N`` posts the SAME idempotency key N times back-to-back (for
idempotent-replay probes: exactly one applied=True expected).
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _api

logger = logging.getLogger("panel_action")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sid")
    parser.add_argument("action_id")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--idem", default=None)
    parser.add_argument("--allow-disabled", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    _api.setup_logging()

    idem = args.idem or f"fleet-{uuid.uuid4().hex}"
    exit_code = 0
    for attempt in range(1, args.repeat + 1):
        status, payload, latency = _api.post_action(
            args.sid,
            args.action_id,
            reason=args.reason,
            idempotency_key=idem,
            allow_disabled=args.allow_disabled,
        )
        logger.info(
            "attempt %d: %s %s -> %d (%.1fs)",
            attempt, args.sid, args.action_id, status, latency,
        )
        _api.emit({"attempt": attempt, "status": status, "payload": payload})
        if status not in (200, 201):
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
