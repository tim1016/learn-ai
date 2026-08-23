"""Review, activate, inspect, or revoke one Signal Program canary pairing.

This broker-free command only changes the local exact-pair admission ledger.
It never starts a bot, resumes a run, submits an order, or contacts Alpaca.

Usage::

    python -m scripts.manage_canary_admission plan \
      --program ema_crossover_signal \
      --account-id PAPER_ACCOUNT_ID \
      --reason "Reviewed EMA paper canary" \
      --output /tmp/ema-canary-plan.json

    python -m scripts.manage_canary_admission apply \
      --plan /tmp/ema-canary-plan.json \
      --confirmation-token TOKEN_FROM_REVIEWED_PLAN
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.canary_admission import CanaryActivationPlan
from app.services.canary_admission import (
    CanaryActivationRefused,
    CanaryAdmissionLedgerError,
    active_canary_pairings,
    apply_canary_activation,
    plan_canary_activation,
    revoke_canary_pairing,
)
from app.services.strategy_validation_manifest import local_strategy_validation_actor
from app.utils.atomic_file import atomic_write_bytes


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.manage_canary_admission",
        description=(
            "Broker-free, proof-bound control of exact Signal Program/account canary pairings."
        ),
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    plan = subparsers.add_parser(
        "plan",
        help="Re-prove current evidence and write a short-lived review plan without activating.",
    )
    _add_pair_arguments(plan)
    plan.add_argument("--reason", required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--confirmation-ttl-ms", type=int, default=120_000)

    apply = subparsers.add_parser(
        "apply",
        help="Confirm a reviewed, unexpired plan and append its activation.",
    )
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--confirmation-token", required=True)

    revoke = subparsers.add_parser(
        "revoke",
        help="Append a revocation for one currently active exact pairing.",
    )
    _add_pair_arguments(revoke)
    revoke.add_argument("--reason", required=True)

    subparsers.add_parser("status", help="Verify the ledger and print active exact pairings.")
    return parser.parse_args(argv)


def _add_pair_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--program", required=True)
    parser.add_argument("--account-id", required=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.operation == "plan":
            result: Any = plan_canary_activation(
                program_key=args.program,
                account_id=args.account_id,
                actor=local_strategy_validation_actor(),
                reason=args.reason,
                confirmation_ttl_ms=args.confirmation_ttl_ms,
            )
            _write_model(args.output, result)
            payload = result.model_dump(mode="json")
        elif args.operation == "apply":
            result = apply_canary_activation(
                plan=_read_plan(args.plan),
                confirmation_token=args.confirmation_token,
            )
            payload = result.model_dump(mode="json")
        elif args.operation == "revoke":
            result = revoke_canary_pairing(
                program_key=args.program,
                account_id=args.account_id,
                actor=local_strategy_validation_actor(),
                reason=args.reason,
            )
            payload = result.model_dump(mode="json")
        else:
            payload = {
                "active_pairings": [
                    {"program_key": program_key, "account_id": account_id}
                    for program_key, account_id in sorted(active_canary_pairings())
                ]
            }
    except (
        CanaryActivationRefused,
        CanaryAdmissionLedgerError,
        OSError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"Canary admission refused: {exc}\n")
        return 2

    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def _read_plan(path: Path) -> CanaryActivationPlan:
    if path.is_symlink():
        raise CanaryActivationRefused("activation plan cannot be a symbolic link")
    return CanaryActivationPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _write_model(path: Path, model: CanaryActivationPlan) -> None:
    encoded = (
        json.dumps(
            model.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, encoded)


if __name__ == "__main__":
    sys.exit(main())
