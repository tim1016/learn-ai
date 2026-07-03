"""Tests for the account instance registry boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live import account_artifacts
from app.engine.live.account_artifacts import (
    AccountArtifactError,
    account_artifacts_root,
)
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    compute_reconcile_namespaces,
    crash_retired_restart_blocking_binding,
    evaluate_account_instance_binding,
    has_account_recovery_evidence_after,
    index_account_instance_bindings,
    latest_account_instance_binding,
    read_account_instance_registry,
    write_account_instance_binding,
)


def _binding(
    *,
    sid: str = "spy-ema-paper-1",
    run_id: str = "run-alpha",
    namespace: str = "learn-ai/spy-ema-paper-1/v1",
    recorded_at_ms: int = 1_700_000_000_000,
) -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id="DU123456",
        strategy_instance_id=sid,
        run_id=run_id,
        bot_order_namespace=namespace,
        lifecycle_state="ACTIVE",
        recorded_at_ms=recorded_at_ms,
        source="host_daemon.start",
    )


def test_read_account_instance_registry_rejects_path_like_account_id(
    tmp_path: Path,
) -> None:
    with pytest.raises(AccountArtifactError, match="invalid account_id"):
        read_account_instance_registry(tmp_path, "DU.123456")


def test_account_instance_registry_accepts_current_binding(tmp_path: Path) -> None:
    binding = _binding()

    path = write_account_instance_binding(tmp_path, binding)

    assert path == tmp_path / "accounts" / "DU123456" / "instance_registry.jsonl"
    assert read_account_instance_registry(tmp_path, "DU123456") == [binding]
    gate = evaluate_account_instance_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="spy-ema-paper-1",
        run_id="run-alpha",
        bot_order_namespace="learn-ai/spy-ema-paper-1/v1",
    )
    assert gate.status == "pass"
    assert gate.operator_next_step == "GATE_PASSING"


def test_compute_reconcile_namespaces_splits_owned_from_active_siblings(tmp_path: Path) -> None:
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="spy",
            run_id="run-spy",
            namespace="learn-ai/spy/v1",
            recorded_at_ms=2,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="spy",
            run_id="run-old",
            namespace="learn-ai/spy-old/v1",
            recorded_at_ms=1,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="aapl",
            run_id="run-aapl",
            namespace="learn-ai/aapl/v1",
            recorded_at_ms=4,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="retired",
            run_id="run-retired",
            namespace="learn-ai/retired/v1",
            recorded_at_ms=3,
        ).model_copy(update={"lifecycle_state": "RETIRED"}),
    )

    owned, siblings = compute_reconcile_namespaces(
        artifacts_root=tmp_path,
        account_id="DU123456",
        current_namespace="learn-ai/aapl/v1",
    )

    assert owned == frozenset({"learn-ai/aapl/v1"})
    assert siblings == frozenset({"learn-ai/spy/v1"})


def test_compute_reconcile_namespaces_drops_later_retired_and_wrong_account_bindings(
    tmp_path: Path,
) -> None:
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="retiring-spy",
            run_id="run-active",
            namespace="learn-ai/retiring-spy/v1",
            recorded_at_ms=1,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="retiring-spy",
            run_id="run-retired",
            namespace="learn-ai/retiring-spy/v1",
            recorded_at_ms=2,
        ).model_copy(update={"lifecycle_state": "RETIRED"}),
    )
    registry_path = account_artifacts_root(tmp_path, "DU123456") / "instance_registry.jsonl"
    with open(registry_path, "a", encoding="utf-8") as fh:
        fh.write(
            _binding(
                sid="wrong-account",
                run_id="run-wrong",
                namespace="learn-ai/wrong-account/v1",
                recorded_at_ms=3,
            ).model_copy(update={"account_id": "DU999999"}).model_dump_json()
            + "\n"
        )

    owned, siblings = compute_reconcile_namespaces(
        artifacts_root=tmp_path,
        account_id="DU123456",
        current_namespace="learn-ai/aapl/v1",
    )

    assert owned == frozenset({"learn-ai/aapl/v1"})
    assert siblings == frozenset()


def test_latest_account_instance_binding_uses_later_append_on_timestamp_tie() -> None:
    active = _binding(recorded_at_ms=1_700_000_000_000)
    retired = active.model_copy(
        update={
            "lifecycle_state": "RETIRED",
            "source": "host_daemon.process_crashed",
        }
    )

    latest = latest_account_instance_binding(
        [active, retired],
        account_id="DU123456",
        strategy_instance_id="spy-ema-paper-1",
    )

    assert latest == retired


def test_index_account_instance_bindings_filters_account_and_tie_breaks_by_append_order() -> None:
    active = _binding(recorded_at_ms=1_700_000_000_000)
    wrong_account = _binding(
        sid="wrong-account",
        namespace="learn-ai/wrong-account/v1",
        recorded_at_ms=1_700_000_000_500,
    ).model_copy(update={"account_id": "DU999999"})
    retired = active.model_copy(
        update={
            "lifecycle_state": "RETIRED",
            "source": "host_daemon.process_crashed",
        }
    )

    binding_index = index_account_instance_bindings(
        [active, wrong_account, retired],
        account_id="DU123456",
    )

    assert binding_index.latest_by_instance == {"spy-ema-paper-1": retired}
    assert binding_index.latest_by_namespace == {
        "learn-ai/spy-ema-paper-1/v1": retired,
    }
    assert binding_index.active_by_namespace == {}
    assert binding_index.duplicate_active_namespaces == frozenset()
    with pytest.raises(TypeError):
        binding_index.latest_by_instance["mutated"] = active


def test_index_account_instance_bindings_groups_duplicate_active_namespace() -> None:
    first = _binding(
        sid="spy-a",
        run_id="run-a",
        namespace="learn-ai/shared/v1",
        recorded_at_ms=1_700_000_000_000,
    )
    second = _binding(
        sid="spy-b",
        run_id="run-b",
        namespace="learn-ai/shared/v1",
        recorded_at_ms=1_700_000_000_001,
    )

    binding_index = index_account_instance_bindings([first, second], account_id="DU123456")

    assert binding_index.active_by_namespace == {
        "learn-ai/shared/v1": (first, second),
    }
    assert binding_index.duplicate_active_namespaces == frozenset({"learn-ai/shared/v1"})


def test_has_account_recovery_evidence_after_requires_later_recovery_event() -> None:
    crash_at_ms = 1_700_000_000_000

    assert has_account_recovery_evidence_after(
        [
            {
                "event_type": "account_instance_binding_recorded",
                "ts_ms": crash_at_ms + 1,
            },
            {
                "event_type": "account_recovery_proof_recorded",
                "ts_ms": crash_at_ms,
            },
        ],
        crash_at_ms,
    ) is False
    assert has_account_recovery_evidence_after(
        [
            {
                "event_type": "account_recovery_proof_recorded",
                "ts_ms": crash_at_ms + 1,
            },
        ],
        crash_at_ms,
    ) is True


def test_crash_retired_restart_recovery_blocks_without_later_proof(tmp_path: Path) -> None:
    active = _binding(recorded_at_ms=1_700_000_000_000)
    retired = active.model_copy(
        update={
            "lifecycle_state": "RETIRED",
            "recorded_at_ms": 1_700_000_010_000,
            "source": "host_daemon.process_crashed",
        }
    )
    write_account_instance_binding(tmp_path, active)
    write_account_instance_binding(tmp_path, retired)

    blocking_binding = crash_retired_restart_blocking_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="spy-ema-paper-1",
    )

    assert blocking_binding == retired


def test_crash_retired_restart_recovery_allows_after_later_proof(tmp_path: Path) -> None:
    retired = _binding(
        recorded_at_ms=1_700_000_010_000,
    ).model_copy(
        update={
            "lifecycle_state": "RETIRED",
            "source": "host_daemon.process_crashed",
        }
    )
    write_account_instance_binding(tmp_path, retired)
    account_artifacts.append_account_event(
        tmp_path,
        "DU123456",
        {
            "event_type": "account_recovery_proof_recorded",
            "recorded_at_ms": 1_700_000_010_001,
            "recovery_id": "proof-1",
        },
    )

    blocking_binding = crash_retired_restart_blocking_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="spy-ema-paper-1",
    )

    assert blocking_binding is None


def test_crash_retired_restart_recovery_allows_non_crash_retirement(tmp_path: Path) -> None:
    retired = _binding().model_copy(
        update={
            "lifecycle_state": "RETIRED",
            "source": "host_daemon.stop_requested",
        }
    )
    write_account_instance_binding(tmp_path, retired)

    blocking_binding = crash_retired_restart_blocking_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="spy-ema-paper-1",
    )

    assert blocking_binding is None


def test_account_instance_registry_blocks_unknown_instance(tmp_path: Path) -> None:
    gate = evaluate_account_instance_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="missing-instance",
        run_id="run-alpha",
        bot_order_namespace="learn-ai/missing-instance/v1",
    )

    assert gate.status == "block"
    assert gate.operator_reason == "ACCOUNT_REGISTRY_UNKNOWN_INSTANCE"


def test_account_instance_registry_blocks_stale_run_binding(tmp_path: Path) -> None:
    write_account_instance_binding(tmp_path, _binding(run_id="run-alpha"))

    gate = evaluate_account_instance_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="spy-ema-paper-1",
        run_id="run-beta",
        bot_order_namespace="learn-ai/spy-ema-paper-1/v1",
    )

    assert gate.status == "block"
    assert gate.operator_reason == "ACCOUNT_REGISTRY_STALE_RUN"


def test_account_instance_registry_blocks_duplicate_namespace(tmp_path: Path) -> None:
    write_account_instance_binding(
        tmp_path,
        _binding(sid="spy-a", run_id="run-a", namespace="learn-ai/shared/v1"),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="spy-b",
            run_id="run-b",
            namespace="learn-ai/shared/v1",
            recorded_at_ms=1_700_000_000_100,
        ),
    )

    gate = evaluate_account_instance_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="spy-b",
        run_id="run-b",
        bot_order_namespace="learn-ai/shared/v1",
    )

    assert gate.status == "block"
    assert gate.operator_reason == "ACCOUNT_REGISTRY_DUPLICATE_NAMESPACE"
