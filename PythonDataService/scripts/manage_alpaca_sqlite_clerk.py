"""Offline operator CLI for Alpaca SQLite Clerk recovery and cutover."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite.catalog_quarantine import (
    CatalogArtifactEvidence,
    CatalogQuarantinePlan,
    apply_catalog_quarantine,
    plan_catalog_quarantine,
)
from app.broker.alpaca.clerk.sqlite.cutover import (
    BrokerCutoverEvidence,
    CutoverInitializationEvidence,
    CutoverPlan,
    LegacyArtifactEvidence,
    RunnerBotEvidence,
    apply_cutover,
    initialize_cutover_authority,
    plan_cutover,
)
from app.broker.alpaca.clerk.sqlite.database_verification import DatabaseVerification
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.broker.alpaca.clerk.sqlite.recovery import (
    ResetBrokerProof,
    create_verified_backup,
    preserve_and_rebuild_from_mirror,
    reset_authority,
    restore_verified_backup,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.manage_alpaca_sqlite_clerk",
        description="Broker-free recovery and human-confirmed cutover tooling.",
    )
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument("--account-id", required=True)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("backup")
    restore = subparsers.add_parser("restore")
    restore.add_argument("--bundle", type=Path, required=True)
    subparsers.add_parser("rebuild")

    reset = subparsers.add_parser("reset")
    _add_reset_evidence_arguments(reset)

    initialize = subparsers.add_parser("cutover-initialize")
    _add_cutover_evidence_arguments(initialize)

    plan = subparsers.add_parser("cutover-plan")
    _add_cutover_evidence_arguments(plan)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--confirmation-ttl-ms", type=int, default=120_000)

    apply = subparsers.add_parser("cutover-apply")
    _add_cutover_evidence_arguments(apply)
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--confirmation-token", required=True)

    catalog_plan = subparsers.add_parser("catalog-quarantine-plan")
    catalog_plan.add_argument("--runner-artifacts-root", type=Path, required=True)
    catalog_plan.add_argument("--output", type=Path, required=True)
    catalog_plan.add_argument("--max-candidates", type=int, required=True)
    catalog_plan.add_argument("--max-total-bytes", type=int, required=True)
    catalog_plan.add_argument("--confirmation-ttl-ms", type=int, default=120_000)

    catalog_apply = subparsers.add_parser("catalog-quarantine-apply")
    catalog_apply.add_argument("--runner-artifacts-root", type=Path, required=True)
    catalog_apply.add_argument("--plan", type=Path, required=True)
    catalog_apply.add_argument("--confirmation-token", required=True)
    return parser.parse_args(argv)


def _add_broker_evidence_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--broker-evidence", type=Path, required=True)
    parser.add_argument("--max-evidence-age-ms", type=int, required=True)


def _add_reset_evidence_arguments(parser: argparse.ArgumentParser) -> None:
    _add_broker_evidence_arguments(parser)
    parser.add_argument("--expected-bot", action="append", default=[])
    parser.add_argument("--stopped-bot", action="append", default=[])


def _add_cutover_evidence_arguments(parser: argparse.ArgumentParser) -> None:
    _add_broker_evidence_arguments(parser)
    parser.add_argument("--runner-artifacts-root", type=Path, required=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    common = {"account_id": args.account_id, "artifacts_root": args.artifacts_root}
    if args.operation == "backup":
        result: Any = create_verified_backup(**common)
    elif args.operation == "restore":
        result = restore_verified_backup(**common, bundle_path=args.bundle)
    elif args.operation == "rebuild":
        result = preserve_and_rebuild_from_mirror(**common)
    elif args.operation == "reset":
        result = reset_authority(
            **common,
            broker_proof=_read_reset_evidence(
                args.broker_evidence, args.account_id
            ),
            expected_strategy_instance_ids=args.expected_bot,
            stopped_strategy_instance_ids=args.stopped_bot,
            max_proof_age_ms=args.max_evidence_age_ms,
        )
    elif args.operation == "cutover-initialize":
        result = initialize_cutover_authority(
            **common,
            runner_artifacts_root=args.runner_artifacts_root,
            broker_evidence=_read_cutover_evidence(
                args.broker_evidence, args.account_id
            ),
            max_broker_evidence_age_ms=args.max_evidence_age_ms,
        )
    elif args.operation == "cutover-plan":
        result = plan_cutover(
            **common,
            runner_artifacts_root=args.runner_artifacts_root,
            broker_evidence=_read_cutover_evidence(
                args.broker_evidence, args.account_id
            ),
            max_broker_evidence_age_ms=args.max_evidence_age_ms,
            confirmation_ttl_ms=args.confirmation_ttl_ms,
        )
        atomic_write_json(args.output, asdict(result))
    elif args.operation == "cutover-apply":
        result = apply_cutover(
            plan=_read_plan(args.plan),
            confirmation_token=args.confirmation_token,
            artifacts_root=args.artifacts_root,
            runner_artifacts_root=args.runner_artifacts_root,
            broker_evidence=_read_cutover_evidence(
                args.broker_evidence, args.account_id
            ),
            max_broker_evidence_age_ms=args.max_evidence_age_ms,
        )
    elif args.operation == "catalog-quarantine-plan":
        result = plan_catalog_quarantine(
            **common,
            runner_artifacts_root=args.runner_artifacts_root,
            max_candidates=args.max_candidates,
            max_total_bytes=args.max_total_bytes,
            confirmation_ttl_ms=args.confirmation_ttl_ms,
        )
        atomic_write_json(args.output, asdict(result))
    else:
        catalog_plan = _read_catalog_quarantine_plan(args.plan)
        if catalog_plan.account_id != args.account_id:
            raise ValueError("catalog quarantine plan account does not match CLI account")
        result = apply_catalog_quarantine(
            plan=catalog_plan,
            confirmation_token=args.confirmation_token,
            artifacts_root=args.artifacts_root,
            runner_artifacts_root=args.runner_artifacts_root,
        )
    sys.stdout.write(json.dumps(_jsonable(result), sort_keys=True) + "\n")
    return 0


def _read_cutover_evidence(path: Path, account_id: str) -> BrokerCutoverEvidence:
    payload = _read_json_object(path)
    required = {
        "account_id",
        "account_mode",
        "observed_at_ms",
        "proof_reference",
        "positions",
        "open_order_ids",
    }
    if set(payload) != required:
        raise ValueError("cutover broker evidence fields do not match schema version 1")
    if payload.get("account_id") != account_id:
        raise ValueError("broker evidence account_id does not match CLI account")
    return BrokerCutoverEvidence(
        account_id=payload["account_id"],
        account_mode=payload["account_mode"],
        observed_at_ms=payload["observed_at_ms"],
        proof_reference=payload["proof_reference"],
        positions=payload.get("positions", {}),
        open_order_ids=tuple(payload.get("open_order_ids", ())),
    )


def _read_reset_evidence(path: Path, account_id: str) -> ResetBrokerProof:
    payload = _read_json_object(path)
    required = {
        "account_id",
        "observed_at_ms",
        "proof_reference",
        "positions",
        "open_order_ids",
    }
    if set(payload) != required:
        raise ValueError("reset broker evidence fields do not match schema version 1")
    if payload.get("account_id") != account_id:
        raise ValueError("broker evidence account_id does not match CLI account")
    return ResetBrokerProof(
        account_id=payload["account_id"],
        observed_at_ms=payload["observed_at_ms"],
        proof_reference=payload["proof_reference"],
        positions=payload.get("positions", {}),
        open_order_ids=tuple(payload.get("open_order_ids", ())),
    )


def _read_plan(path: Path) -> CutoverPlan:
    payload = _read_json_object(path)
    required = {
        "schema_version",
        "plan_id",
        "confirmation_token",
        "account_id",
        "created_at_ms",
        "expires_at_ms",
        "initialization",
        "database",
        "broker_evidence",
        "runner_roster",
        "legacy_artifacts",
    }
    if set(payload) != required or payload.get("schema_version") != 3:
        raise ValueError("cutover plan fields do not match schema version 3")
    return CutoverPlan(
        schema_version=payload["schema_version"],
        plan_id=payload["plan_id"],
        confirmation_token=payload["confirmation_token"],
        account_id=payload["account_id"],
        created_at_ms=payload["created_at_ms"],
        expires_at_ms=payload["expires_at_ms"],
        initialization=CutoverInitializationEvidence(**payload["initialization"]),
        database=DatabaseVerification(**payload["database"]),
        broker_evidence=BrokerCutoverEvidence(
            **{
                **payload["broker_evidence"],
                "open_order_ids": tuple(payload["broker_evidence"]["open_order_ids"]),
            }
        ),
        runner_roster=tuple(
            RunnerBotEvidence(**item) for item in payload["runner_roster"]
        ),
        legacy_artifacts=tuple(
            LegacyArtifactEvidence(**item) for item in payload["legacy_artifacts"]
        ),
    )


def _read_catalog_quarantine_plan(path: Path) -> CatalogQuarantinePlan:
    payload = _read_json_object(path)
    required = {
        "schema_version",
        "plan_id",
        "confirmation_token",
        "account_id",
        "created_at_ms",
        "expires_at_ms",
        "max_candidates",
        "max_total_bytes",
        "database",
        "registered_strategy_instance_ids",
        "candidates",
    }
    if set(payload) != required or payload.get("schema_version") != 1:
        raise ValueError("catalog quarantine plan fields do not match schema version 1")
    return CatalogQuarantinePlan(
        schema_version=payload["schema_version"],
        plan_id=payload["plan_id"],
        confirmation_token=payload["confirmation_token"],
        account_id=payload["account_id"],
        created_at_ms=payload["created_at_ms"],
        expires_at_ms=payload["expires_at_ms"],
        max_candidates=payload["max_candidates"],
        max_total_bytes=payload["max_total_bytes"],
        database=DatabaseVerification(**payload["database"]),
        registered_strategy_instance_ids=tuple(payload["registered_strategy_instance_ids"]),
        candidates=tuple(
            CatalogArtifactEvidence(**item) for item in payload["candidates"]
        ),
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


if __name__ == "__main__":
    sys.exit(main())
