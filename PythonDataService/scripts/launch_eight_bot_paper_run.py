"""Deploy and start 8 deployment-validation paper bots for DUM284968.

Runs sequentially with a configurable stagger (default 60 s) so restart-intensity
never trips the account freeze.  Each bot gets its own equity symbol; all use
FixedShares(1) sizing and the deployment_validation strategy.

Usage::

    # Dry run — print what would be deployed without calling the API
    python -m scripts.launch_eight_bot_paper_run --dry-run

    # Live deploy (daemon must be up, IBKR Gateway connected, account CLEAN)
    python -m scripts.launch_eight_bot_paper_run

    # Custom date prefix (default = today YYYYMMDD in America/Chicago)
    python -m scripts.launch_eight_bot_paper_run --date 20260730

    # Shorter stagger (seconds between each deploy+start call)
    python -m scripts.launch_eight_bot_paper_run --stagger 30

Prerequisites:
    - Host daemon running  (./start-live-daemon.sh --background)
    - IBKR Gateway up and paper account DUM284968 connected
    - Account epoch CLEAN and no active suspension
      (curl -s localhost:8000/api/live-instances/account | python3 -m json.tool)
    - All 8 target strategy_instance_ids are off-roster or don't exist yet
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

_CONTROL_SECRET = os.environ.get("DATA_PLANE_CONTROL_SECRET", "")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC_PATH = (
    _REPO_ROOT
    / "PythonDataService"
    / "app"
    / "engine"
    / "strategy"
    / "spec"
    / "fixtures"
    / "deployment_validation.spec.json"
)
_QC_AUDIT_PATH = _REPO_ROOT / "references" / "qc-shadow" / "DeploymentValidationAlgorithm.py"

# Content-addressed QC cloud backtest ID for deployment_validation (pinned).
_QC_CLOUD_BACKTEST_ID = "d2fe45a7142e88575f6fbd75229f8681"

# Eight independent equity tickers — one per bot.
_SYMBOLS = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META")

_DATA_PLANE = "http://127.0.0.1:8000"

logger = logging.getLogger(__name__)


def _action_for_symbol(symbol: str) -> dict:
    return {
        "on_enter": [
            {
                "instrument": {"kind": "stock", "underlying": symbol},
                "leg_id": "leg_1",
                "position": "long",
                "qty_ratio": 1,
            }
        ],
        "on_exit": [{"entry_leg_id": "leg_1", "kind": "close_leg"}],
    }


def _deploy_payload(symbol: str, date_prefix: str, now_ms: int) -> dict:
    sid = f"dv-{date_prefix}-{symbol.lower()}"
    return {
        "strategy_spec_path": str(_SPEC_PATH),
        "qc_audit_copy_path": str(_QC_AUDIT_PATH),
        "qc_cloud_backtest_id": _QC_CLOUD_BACKTEST_ID,
        "start_date_ms": now_ms,
        "strategy_instance_id": sid,
        "strategy_key": "deployment_validation",
        "live_config": {
            "symbol": symbol,
            "action": _action_for_symbol(symbol),
            "sizing": {"kind": "FixedShares", "value": 1},
        },
        "start": True,
        "start_options": {
            "readonly": False,
            "hydrate_policy": "optional",
            "strategy": "deployment_validation",
            "max_orders_per_day": 100,
            "ibkr_host": "127.0.0.1",
        },
    }


def _today_date_prefix() -> str:
    ct = datetime.now(ZoneInfo("America/Chicago"))
    return ct.strftime("%Y%m%d")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.launch_eight_bot_paper_run",
        description="Deploy and start 8 paper bots on DUM284968.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="YYYYMMDD date prefix for bot names (default: today in America/Chicago)",
    )
    parser.add_argument(
        "--stagger",
        type=int,
        default=60,
        metavar="SECONDS",
        help="Seconds to wait between each deploy+start (default: 60)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payloads and exit without calling the API",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)

    date_prefix = args.date or _today_date_prefix()
    stagger_s = args.stagger

    if not _SPEC_PATH.exists():
        logger.error("spec file not found: %s", _SPEC_PATH)
        return 1
    if not _QC_AUDIT_PATH.exists():
        logger.error("QC audit copy not found: %s", _QC_AUDIT_PATH)
        return 1

    now_ms = time.time_ns() // 1_000_000
    bots = [
        {"symbol": sym, "payload": _deploy_payload(sym, date_prefix, now_ms)}
        for sym in _SYMBOLS
    ]

    if args.dry_run:
        for bot in bots:
            print(f"\n--- {bot['symbol']} ---")
            print(json.dumps(bot["payload"], indent=2))
        print(f"\n[dry-run] would deploy {len(bots)} bots with {stagger_s}s stagger")
        return 0

    results: list[dict] = []
    headers = {"X-Data-Plane-Control-Secret": _CONTROL_SECRET} if _CONTROL_SECRET else {}
    with httpx.Client(base_url=_DATA_PLANE, timeout=120.0, headers=headers) as client:
        for i, bot in enumerate(bots):
            symbol = bot["symbol"]
            payload = bot["payload"]
            sid = payload["strategy_instance_id"]

            if i > 0:
                logger.info("waiting %ds before deploying %s…", stagger_s, symbol)
                time.sleep(stagger_s)

            logger.info("[%d/%d] deploying %s (%s)", i + 1, len(bots), sid, symbol)
            try:
                resp = client.post("/api/live-instances", json=payload)
            except httpx.RequestError as exc:
                logger.error("request failed for %s: %s", symbol, exc)
                results.append({"symbol": symbol, "sid": sid, "error": str(exc)})
                continue

            if resp.status_code not in (200, 201):
                logger.error(
                    "%s deploy rejected: HTTP %d — %s",
                    symbol,
                    resp.status_code,
                    resp.text[:500],
                )
                results.append(
                    {"symbol": symbol, "sid": sid, "http_status": resp.status_code, "body": resp.text[:500]}
                )
                continue

            body = resp.json()
            run_id = body.get("run_id", "?")
            created = body.get("created", "?")
            start_accepted = (body.get("start") or {}).get("accepted", "?")
            logger.info(
                "%s deployed: run_id=%s created=%s start.accepted=%s",
                sid,
                run_id[:12] + "…",
                created,
                start_accepted,
            )
            results.append(
                {"symbol": symbol, "sid": sid, "run_id": run_id, "created": created, "start_accepted": start_accepted}
            )

    print("\n=== launch summary ===")
    for r in results:
        if "error" in r or "http_status" in r:
            print(f"  FAIL  {r['symbol']:6s} {r.get('sid','')} — {r.get('error') or r.get('http_status')}")
        else:
            print(
                f"  OK    {r['symbol']:6s} {r['sid']} run={r['run_id'][:12]}… start={r['start_accepted']}"
            )

    failures = sum(1 for r in results if "error" in r or "http_status" in r)
    if failures:
        logger.error("%d/%d bots failed to deploy", failures, len(results))
        return 1
    logger.info("all %d bots deployed and started", len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
