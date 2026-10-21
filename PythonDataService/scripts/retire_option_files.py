"""Dry-run, then apply an owner-approved retirement plan (#3432).

The first invocation writes a reviewable JSON plan without deleting anything.
Post that exact output on #3432 and obtain owner approval before applying.
The approval JSON names the plan digest, approval comment and compatibility
checks; it does not replace those checks. Keep the original backup and its
successful isolated restore receipt through the bounded rollback interval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from app.data_lake import catalog_client
from app.data_lake.option_retirement import (
    RetirementApproval,
    RetirementPlan,
    RetirementRefusedError,
    apply_retirement,
    legacy_sources,
    plan_retirement,
)
from app.data_lake.root_identity import read_marker
from app.research.runs.hashing import canonical_json
from app.utils.atomic_file import atomic_write_bytes
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


async def _run(args: argparse.Namespace) -> int:
    marker = read_marker(args.raw_root.parent.parent)
    if marker is None:
        raise RetirementRefusedError("raw root has no root identity marker")
    await catalog_client.init_pool()
    try:
        if args.apply:
            stored = json.loads(args.plan.read_bytes())
            plan = RetirementPlan.model_validate(stored["plan"])
            if stored["plan_sha256"] != plan.digest:
                raise RetirementRefusedError("stored dry-run digest does not match its plan")
            if args.approval is None or args.journal is None:
                raise RetirementRefusedError("apply requires the owner's approval receipt and a persistent journal")
            count = await apply_retirement(
                plan=plan, approval=RetirementApproval.model_validate_json(args.approval.read_bytes()),
                mode_root=args.raw_root, generation=args.generation, restore_receipt=args.restore_receipt,
                journal=args.journal, now_ms=now_ms_utc(),
            )
            logger.info("Approved retirement finished", extra={"action": "option_retirement_complete", "files": count})
        else:
            if args.plan.exists():
                raise RetirementRefusedError("dry run will not overwrite an existing reviewable plan")
            sources = await legacy_sources(marker.data_root_id)
            plan = await asyncio.to_thread(
                plan_retirement, mode_root=args.raw_root, sources=sources, generation=args.generation,
                restore_receipt=args.restore_receipt, now_ms=now_ms_utc(),
            )
            atomic_write_bytes(args.plan, canonical_json({
                "plan_sha256": plan.digest, "plan": plan.model_dump(mode="json"),
            }).encode())
            logger.info("Retirement dry run saved; owner review is required", extra={
                "action": "option_retirement_dry_run", "files": len(plan.sources),
                "plan_sha256": plan.digest, "output": str(args.plan),
            })
    finally:
        await catalog_client.close_pool()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--restore-receipt", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        return asyncio.run(_run(args))
    except (RetirementRefusedError, OSError, ValueError) as exc:
        logger.error("Retirement refused", extra={"action": "option_retirement_refused", "reason": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
