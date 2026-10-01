"""Tests for the account instance registry boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.account_artifacts import AccountArtifactError
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    index_account_instance_bindings,
    read_account_instance_registry,
)
from tests._helpers.legacy_ibkr_artifacts import (
    write_historical_account_binding,
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

    path = write_historical_account_binding(tmp_path, binding)

    assert path == tmp_path / "accounts" / "DU123456" / "instance_registry.jsonl"
    assert read_account_instance_registry(tmp_path, "DU123456") == [binding]


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
