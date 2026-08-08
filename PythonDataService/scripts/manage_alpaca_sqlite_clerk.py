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
from app.broker.alpaca.clerk.sqlite.dev_reset import developer_clean_slate_reset
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.broker.alpaca.clerk.sqlite.recovery import (
    ProcessStopProof,
    ResetBrokerProof,
    create_verified_backup,
    preserve_and_rebuild_from_mirror,
    reset_authority,
    restore_verified_backup,
    verify_authority_head,
)
from app.broker.alpaca.config import get_alpaca_settings


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.manage_alpaca_sqlite_clerk",
        description="Broker-free recovery and human-confirmed cutover tooling.",
    )
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument("--account-id", required=True)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("backup")
    subparsers.add_parser("verify")
    restore = subparsers.add_parser("restore")
    restore.add_argument("--bundle", type=Path, required=True)
    _add_process_stop_evidence_arguments(restore)
    rebuild = subparsers.add_parser("rebuild")
    _add_process_stop_evidence_arguments(rebuild)

    reset = subparsers.add_parser("reset")
    _add_reset_evidence_arguments(reset)
    _add_process_stop_evidence_arguments(reset)

    dev_reset = subparsers.add_parser(
        "dev-reset",
        help="Move disposable paper authority aside for a clean development slate.",
    )
    dev_reset.add_argument(
        "--runner-artifacts-root",
        type=Path,
        required=True,
        help="Root containing this paper account's disposable runner catalogs.",
    )

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


def _add_process_stop_evidence_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--process-stop-evidence", type=Path)
    parser.add_argument("--max-process-stop-evidence-age-ms", type=int, default=120_000)


def _add_cutover_evidence_arguments(parser: argparse.ArgumentParser) -> None:
    _add_broker_evidence_arguments(parser)
    parser.add_argument("--runner-artifacts-root", type=Path, required=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    common = {"account_id": args.account_id, "artifacts_root": args.artifacts_root}
    if args.operation == "backup":
        result: Any = create_verified_backup(**common)
    elif args.operation == "verify":
        result = verify_authority_head(**common)
    elif args.operation == "restore":
        result = restore_verified_backup(
            **common,
            bundle_path=args.bundle,
            process_stop_proof=_read_process_stop_evidence(
                args.process_stop_evidence,
                args.account_id,
            ),
            max_process_stop_proof_age_ms=args.max_process_stop_evidence_age_ms,
        )
    elif args.operation == "rebuild":
        result = preserve_and_rebuild_from_mirror(
            **common,
            process_stop_proof=_read_process_stop_evidence(
                args.process_stop_evidence,
                args.account_id,
            ),
            max_process_stop_proof_age_ms=args.max_process_stop_evidence_age_ms,
        )
    elif args.operation == "reset":
        result = reset_authority(
            **common,
            broker_proof=_read_reset_evidence(
                args.broker_evidence, args.account_id
            ),
            expected_strategy_instance_ids=args.expected_bot,
            stopped_strategy_instance_ids=args.stopped_bot,
            max_proof_age_ms=args.max_evidence_age_ms,
            process_stop_proof=_read_process_stop_evidence(
                args.process_stop_evidence,
                args.account_id,
            ),
            max_process_stop_proof_age_ms=args.max_process_stop_evidence_age_ms,
        )
    elif args.operation == "dev-reset":
        result = developer_clean_slate_reset(
            **common,
            runner_artifacts_root=args.runner_artifacts_root,
            account_mode=get_alpaca_settings().mode,
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


def _read_process_stop_evidence(
    path: Path | None,
    account_id: str,
) -> ProcessStopProof | None:
    if path is None:
        return None
    payload = _read_json_object(path)
    required = {"account_id", "observed_at_ms", "proof_reference"}
    if set(payload) != required:
        raise ValueError("process-stop evidence fields do not match schema version 1")
    if payload.get("account_id") != account_id:
        raise ValueError("process-stop evidence account_id does not match CLI account")
    return ProcessStopProof(
        account_id=payload["account_id"],
        observed_at_ms=payload["observed_at_ms"],
        proof_reference=payload["proof_reference"],
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
    _require_scalar_fields(
        payload,
        {
            "schema_version": int,
            "plan_id": str,
            "confirmation_token": str,
            "account_id": str,
            "created_at_ms": int,
            "expires_at_ms": int,
            "max_candidates": int,
            "max_total_bytes": int,
        },
        label="catalog quarantine plan",
    )
    database_payload = _require_exact_object(
        payload["database"],
        {
            "account_id",
            "authority_generation",
            "db_identity_token",
            "schema_version",
            "control_revision",
            "transition_count",
            "last_sequence",
            "last_row_hash",
        },
        label="catalog quarantine database",
    )
    _require_scalar_fields(
        database_payload,
        {
            "account_id": str,
            "authority_generation": int,
            "db_identity_token": str,
            "schema_version": int,
            "control_revision": int,
            "transition_count": int,
            "last_sequence": int,
            "last_row_hash": str,
        },
        label="catalog quarantine database",
    )
    if database_payload["account_id"] != payload["account_id"]:
        raise ValueError("catalog quarantine database belongs to another account")
    registered_ids = payload["registered_strategy_instance_ids"]
    if not isinstance(registered_ids, list) or not all(
        isinstance(item, str) and item for item in registered_ids
    ):
        raise ValueError(
            "catalog quarantine registered_strategy_instance_ids must be a list of strings"
        )
    candidate_payloads = payload["candidates"]
    if not isinstance(candidate_payloads, list):
        raise ValueError("catalog quarantine candidates must be a list")
    candidates: list[CatalogArtifactEvidence] = []
    for index, item in enumerate(candidate_payloads):
        candidate_payload = _require_exact_object(
            item,
            {"strategy_instance_id", "relative_path", "sha256", "size_bytes"},
            label=f"catalog quarantine candidate {index}",
        )
        _require_scalar_fields(
            candidate_payload,
            {
                "strategy_instance_id": str,
                "relative_path": str,
                "sha256": str,
                "size_bytes": int,
            },
            label=f"catalog quarantine candidate {index}",
        )
        candidates.append(CatalogArtifactEvidence(**candidate_payload))
    return CatalogQuarantinePlan(
        schema_version=payload["schema_version"],
        plan_id=payload["plan_id"],
        confirmation_token=payload["confirmation_token"],
        account_id=payload["account_id"],
        created_at_ms=payload["created_at_ms"],
        expires_at_ms=payload["expires_at_ms"],
        max_candidates=payload["max_candidates"],
        max_total_bytes=payload["max_total_bytes"],
        database=DatabaseVerification(**database_payload),
        registered_strategy_instance_ids=tuple(registered_ids),
        candidates=tuple(candidates),
    )


def _require_exact_object(
    value: Any,
    required: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError(f"{label} fields do not match the required schema")
    return value


def _require_scalar_fields(
    payload: dict[str, Any],
    required_types: dict[str, type],
    *,
    label: str,
) -> None:
    for field, required_type in required_types.items():
        value = payload.get(field)
        valid = (
            type(value) is int
            if required_type is int
            else isinstance(value, required_type)
        )
        if not valid or (required_type is str and not value):
            raise ValueError(
                f"{label} field {field!r} must be a non-empty {required_type.__name__}"
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
