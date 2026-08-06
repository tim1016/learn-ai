"""Deterministic scale fixtures and performance evidence for SQLite Clerk."""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from app.broker.alpaca.clerk.sqlite import schema, writes
from app.broker.alpaca.clerk.sqlite.hashchain import (
    canonical_payload,
    compute_row_hash,
)
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.services.account_custody_qualification import nist_r6_percentile

QualificationProfile = Literal["smoke", "full"]
FIXED_NOW_MS = 1_800_000_000_000
PROFILE_SCALES: dict[QualificationProfile, tuple[tuple[int, int], ...]] = {
    "smoke": ((1, 1_000),),
    "full": ((1, 10_000), (10, 100_000), (100, 1_000_000)),
}
PERFORMANCE_BUDGETS_MS: dict[str, float] = {
    "account_snapshot": 100.0,
    "bot_snapshot": 75.0,
    "timeline_page": 100.0,
}

SMOKE_ADVERSARIAL_TESTS: tuple[str, ...] = (
    "tests/broker/alpaca/clerk/sqlite/test_enter.py::test_intent_commits_before_the_broker_is_ever_called",
    "tests/broker/alpaca/clerk/sqlite/test_enter.py::test_concurrent_duplicate_enter_produces_exactly_one_broker_intent",
    "tests/broker/alpaca/clerk/sqlite/test_enter.py::test_lost_response_resolves_immediately_when_order_is_found",
    "tests/broker/alpaca/clerk/sqlite/test_commands.py::test_state_machine_survives_restart",
    "tests/broker/alpaca/clerk/sqlite/test_exit.py::test_partial_fill_during_cancel_uses_only_the_clerk_proven_remaining_quantity",
    "tests/broker/alpaca/clerk/sqlite/test_exit.py::test_lost_cancel_response_blocks_the_closing_order",
    "tests/broker/alpaca/clerk/sqlite/test_enter.py::test_out_of_order_broker_state_event_does_not_regress_orders_broker_state",
    "tests/broker/alpaca/test_trade_updates.py::test_reconnect_gap_reconcile_pulls_missed_orders",
    "tests/broker/alpaca/clerk/sqlite/test_repository.py::test_corrupt_database_page_fails_closed_on_open",
    "tests/broker/alpaca/clerk/sqlite/test_repository.py::test_tampered_mirror_line_fails_closed_on_rebuild",
    "tests/broker/alpaca/clerk/sqlite/test_repository.py::test_mirror_sequence_gap_fails_closed_on_rebuild",
    "tests/broker/alpaca/clerk/sqlite/test_repository.py::test_rebuild_from_mirror_reconstructs_identical_state",
    "tests/broker/alpaca/clerk/sqlite/test_repository.py::test_substituted_database_rejected_on_identity_mismatch",
    "tests/broker/alpaca/clerk/sqlite/test_repository.py::test_remove_db_after_established_is_not_silently_recreated",
    "tests/broker/alpaca/clerk/sqlite/test_repository.py::test_disk_full_before_mirror_prepare_fails_closed_without_transition",
    "tests/broker/alpaca/clerk/sqlite/test_recovery_tooling.py::test_corrupt_wal_fails_closed_on_startup",
    "tests/broker/alpaca/clerk/sqlite/test_recovery_tooling.py::test_interrupted_backup_keeps_previous_verified_publication",
    "tests/broker/alpaca/clerk/sqlite/test_recovery_tooling.py::test_rebuild_rejects_a_valid_mirror_from_another_account",
    "tests/broker/alpaca/clerk/sqlite/test_recovery_tooling.py::test_flat_reset_rotates_generation_and_preserves_old_authority",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_initialize_refuses_running_roster_without_creating_authority",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_initialize_refuses_empty_runner_roster_without_creating_authority",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_initialize_refuses_tampered_normalized_runner_binding",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_initialize_refuses_broken_binding_symlink",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_initialize_refuses_wrong_clerk_root_without_creating_authority",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_initialize_resumes_verified_inactive_generation_after_receipt_failure",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_initialize_refuses_unrelated_empty_generation_without_cutover_intent",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_initialize_refuses_a_prepared_generation_that_was_later_used",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_initialize_ignores_a_valid_non_alpaca_binding_in_the_shared_runner_root",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_apply_rejects_changed_durable_roster_before_activation",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_apply_rejects_changed_initialization_receipt_before_activation",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_apply_activates_sqlite_and_quarantines_exact_legacy_artifacts",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_apply_rejects_changed_evidence_without_quarantining_or_activating",
    "tests/broker/alpaca/clerk/sqlite/test_cutover.py::test_successful_fixture_cutover_restarts_with_only_sqlite_authority",
)


@dataclass(frozen=True)
class LatencySummary:
    sample_count: int
    p50_ms: float
    p95_ms: float
    max_ms: float


def run_performance_profile(
    profile: QualificationProfile,
    *,
    work_root: Path | None = None,
    scales: tuple[tuple[int, int], ...] | None = None,
) -> dict[str, Any]:
    """Build isolated scale fixtures and benchmark canonical public reads."""
    selected = scales or PROFILE_SCALES[profile]
    host = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
    }
    if work_root is None:
        with tempfile.TemporaryDirectory(prefix="alpaca-sqlite-qualification-") as temporary:
            results = [_run_scale(Path(temporary), bots, rows) for bots, rows in selected]
    else:
        work_root.mkdir(parents=True, exist_ok=True)
        results = [_run_scale(work_root, bots, rows) for bots, rows in selected]
    budget = _evaluate_performance_budgets(results)
    return {
        "schema_version": 1,
        "profile": profile,
        "generated_at_ms": time.time_ns() // 1_000_000,
        "execution_mode": "DETERMINISTIC_FAKE_BROKER_AND_SYNTHETIC_SCALE",
        "broker_dependency": "NONE",
        "host": host,
        "performance_budget": budget,
        "scales": results,
    }


def qualification_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Alpaca SQLite Clerk qualification evidence",
        "",
        f"- Profile: `{report['profile']}`",
        f"- Generated at: `{report['generated_at_ms']}` ms UTC",
        f"- Host: `{report['host']['platform']}`; Python `{report['host']['python']}`",
        "- Broker dependency: none (deterministic fixtures only)",
        f"- Performance budget: `{report['performance_budget']['status']}`",
        "",
        "| Bots | Transitions | Account p95 ms | Bot p95 ms | Timeline p95 ms | DB bytes | WAL bytes | Mirror bytes |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scale in report["scales"]:
        latencies = scale["latencies"]
        sizes = scale["sizes_bytes"]
        lines.append(
            f"| {scale['bot_count']} | {scale['transition_count']} | "
            f"{latencies['account_snapshot']['p95_ms']:.3f} | "
            f"{latencies['bot_snapshot']['p95_ms']:.3f} | "
            f"{latencies['timeline_page']['p95_ms']:.3f} | "
            f"{sizes['database']} | {sizes['wal']} | {sizes['mirror']} |"
        )
    lines.extend(
        [
            "",
            "The scale loader is an offline, batched, hash-chained fixture builder; it is not represented as production capture-before-contact throughput. Capture latency is measured separately through real repository registration commits.",
            "",
        ]
    )
    return "\n".join(lines)


def _evaluate_performance_budgets(scales: list[dict[str, Any]]) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    for scale in scales:
        for operation, limit_ms in PERFORMANCE_BUDGETS_MS.items():
            observed_ms = scale["latencies"][operation]["p95_ms"]
            if observed_ms >= limit_ms:
                violations.append(
                    {
                        "bot_count": scale["bot_count"],
                        "transition_count": scale["transition_count"],
                        "operation": operation,
                        "observed_p95_ms": observed_ms,
                        "required_below_ms": limit_ms,
                    }
                )
    return {
        "status": "PASSED" if not violations else "FAILED",
        "limits_p95_ms_exclusive": PERFORMANCE_BUDGETS_MS,
        "violations": violations,
    }


def _run_scale(root: Path, bot_count: int, transition_count: int) -> dict[str, Any]:
    account_id = f"QUAL-{bot_count}-{transition_count}"
    fixture_root = root / account_id
    capture_samples_us: list[int] = []
    repo = ClerkSqliteRepository.initialize(
        account_id=account_id,
        artifacts_root=fixture_root,
        clock=lambda: FIXED_NOW_MS,
    )
    for index in range(bot_count):
        started = time.perf_counter_ns()
        repo.register_strategy_instance(
            strategy_instance_id=f"bot-{index:03d}",
            symbol=f"S{index:03d}",
            config_hash=f"config-{index:03d}",
        )
        capture_samples_us.append((time.perf_counter_ns() - started) // 1_000)
    db_path = repo.db_path
    mirror_path = repo.mirror_path
    meta = repo.control_meta_snapshot()
    repo.close()
    _load_timeline_fixture(
        db_path=db_path,
        mirror_path=mirror_path,
        bot_count=bot_count,
        target_transition_count=transition_count,
    )
    conn = sqlite3.connect(db_path)
    schema.configure_connection(conn)
    pragmas = {
        name: conn.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("journal_mode", "synchronous", "foreign_keys", "busy_timeout")
    }
    query_plans = _query_plans(conn, strategy_instance_id="bot-000")
    conn.close()
    reader = SqliteClerkProjectionReader(
        db_path=db_path,
        account_id=account_id,
        authority_generation=meta.authority_generation,
        db_identity_token=meta.db_identity_token,
        clock=lambda: FIXED_NOW_MS,
    )
    try:
        account_samples = _measure(lambda: reader.account_snapshot())
        bot_samples = _measure(lambda: reader.bot_snapshot("bot-000"))
        timeline_samples = _measure(lambda: reader.timeline_page(page_size=25))
    finally:
        reader.close()
    sizes = {
        "database": _size(db_path),
        "wal": _size(Path(f"{db_path}-wal")),
        "mirror": _size(mirror_path),
    }
    return {
        "bot_count": bot_count,
        "transition_count": transition_count,
        "row_transaction_mix": {
            "production_registration_commits": bot_count,
            "offline_timeline_fixture_rows": max(0, transition_count - bot_count),
            "offline_batch_size": 10_000,
        },
        "pragmas": pragmas,
        "latencies": {
            "capture_before_contact_registration": asdict(
                _latency_summary(capture_samples_us)
            ),
            "account_snapshot": asdict(_latency_summary(account_samples)),
            "bot_snapshot": asdict(_latency_summary(bot_samples)),
            "timeline_page": asdict(_latency_summary(timeline_samples)),
        },
        "sizes_bytes": sizes,
        "query_plans": query_plans,
    }


def _load_timeline_fixture(
    *, db_path: Path, mirror_path: Path, bot_count: int, target_transition_count: int
) -> None:
    if target_transition_count < bot_count:
        raise ValueError("transition scale cannot be smaller than bot count")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    last = conn.execute(
        "SELECT sequence, row_hash FROM custody_transitions ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(last["sequence"]) if last is not None else 0
    previous_hash = str(last["row_hash"]) if last is not None else "GENESIS"
    with mirror_path.open("a", encoding="utf-8") as mirror:
        while sequence < target_transition_count:
            conn.execute("BEGIN")
            for _ in range(min(10_000, target_transition_count - sequence)):
                sequence += 1
                payload = _timeline_payload(sequence, bot_count)
                payload_canonical = canonical_payload(payload)
                row_hash = compute_row_hash(previous_hash, payload_canonical)
                writes.insert_custody_transition_row(
                    conn,
                    sequence=sequence,
                    prev_hash=previous_hash,
                    row_hash=row_hash,
                    payload=payload,
                )
                _write_mirror_pair(
                    mirror,
                    sequence=sequence,
                    previous_hash=previous_hash,
                    row_hash=row_hash,
                    payload_canonical=payload_canonical,
                )
                previous_hash = row_hash
            conn.commit()
        conn.execute(
            "UPDATE control_meta SET control_revision = ? WHERE id = 1",
            (target_transition_count,),
        )
        conn.commit()
        mirror.flush()
        os.fsync(mirror.fileno())
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def _timeline_payload(sequence: int, bot_count: int) -> dict[str, Any]:
    timestamp = FIXED_NOW_MS + sequence
    return {
        "authority_generation": 1,
        "strategy_instance_id": f"bot-{(sequence - 1) % bot_count:03d}",
        "run_id": None,
        "command_id": None,
        "effect_operation_id": None,
        "order_ref": None,
        "broker_order_id": None,
        "transition_kind": "QUALIFICATION_TIMELINE_SAMPLE",
        "custody_owner": "ACCOUNT_CLERK",
        "execution_authority": "ACCOUNT_CLERK",
        "operation_state": "succeeded",
        "broker_state": None,
        "proof_reference": f"qualification:{sequence}",
        "source_event_at_ms": timestamp - 2,
        "clerk_observed_at_ms": timestamp - 1,
        "recorded_at_ms": timestamp,
        "summary_code": "QUALIFICATION_TIMELINE_SAMPLE",
        "facts_schema_version": 1,
        "facts_json": "{}",
    }


def _write_mirror_pair(
    mirror: Any,
    *,
    sequence: int,
    previous_hash: str,
    row_hash: str,
    payload_canonical: str,
) -> None:
    prepare = {
        "phase": "PREPARE",
        "sequence": sequence,
        "authority_generation": 1,
        "row_hash": row_hash,
        "prev_hash": previous_hash,
        "payload_canonical": payload_canonical,
        "recorded_at_ms": FIXED_NOW_MS + sequence,
    }
    finalize = {
        "phase": "FINALIZE",
        "sequence": sequence,
        "authority_generation": 1,
        "row_hash": row_hash,
        "recorded_at_ms": FIXED_NOW_MS + sequence,
    }
    mirror.write(json.dumps(prepare, sort_keys=True, separators=(",", ":")) + "\n")
    mirror.write(json.dumps(finalize, sort_keys=True, separators=(",", ":")) + "\n")


def _query_plans(conn: sqlite3.Connection, *, strategy_instance_id: str) -> dict[str, list[str]]:
    statements = {
        "account_commands": (
            "SELECT command_id FROM commands ORDER BY updated_at_ms DESC, command_id DESC LIMIT 50",
            (),
        ),
        "bot_operations": (
            "SELECT effect_operation_id FROM effect_operations WHERE strategy_instance_id = ? "
            "ORDER BY updated_at_ms DESC, effect_operation_id DESC LIMIT 50",
            (strategy_instance_id,),
        ),
        "bot_timeline": (
            "SELECT sequence FROM custody_transitions WHERE sequence <= ? AND sequence < ? "
            "AND strategy_instance_id = ? ORDER BY sequence DESC LIMIT 25",
            (2**63 - 1, 2**63 - 1, strategy_instance_id),
        ),
    }
    return {
        name: [str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)]
        for name, (sql, params) in statements.items()
    }


def _measure(operation: Any, *, iterations: int = 12) -> list[int]:
    samples_us: list[int] = []
    operation()
    for _ in range(iterations):
        started = time.perf_counter_ns()
        operation()
        samples_us.append((time.perf_counter_ns() - started) // 1_000)
    return samples_us


def _latency_summary(samples_us: list[int]) -> LatencySummary:
    """Summarize latency via the repository's canonical NIST R6 percentile.

    Formula: Q_p uses NIST R6 rank p(n+1), with microseconds converted to ms.
    Reference: NIST/SEMATECH e-Handbook §7.2.6.2, Percentiles.
    Canonical implementation: app/services/account_custody_qualification.py::nist_r6_percentile.
    Validated against: tests/services/test_account_custody_qualification.py::test_nist_r6_percentiles_match_reference_examples.
    """
    if not samples_us:
        raise ValueError("latency summary requires at least one sample")
    return LatencySummary(
        sample_count=len(samples_us),
        p50_ms=nist_r6_percentile(samples_us, 50) / 1_000,
        p95_ms=nist_r6_percentile(samples_us, 95) / 1_000,
        max_ms=max(samples_us) / 1_000,
    )


def _size(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0
