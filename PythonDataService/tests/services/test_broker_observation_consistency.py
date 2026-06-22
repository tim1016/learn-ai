"""PRD #619-D4 — unit tests for the broker-observation-consistency resolver.

Covers every verdict cell of the four-way classifier and the two
edge cases (mode mismatch outranking account mismatch, and missing
configured-mode falling back to the standard account-comparison
path).  The router-level wiring is exercised in the operator-surface
projection tests.
"""

from __future__ import annotations

import pytest

from app.broker.runtime_snapshot import BrokerRuntimeSnapshot
from app.engine.live.engine_runtime import BrokerBlock
from app.services.broker_observation_consistency import (
    REASON_CODES,
    evaluate_broker_observation_consistency,
)

_NOW_MS = 1_700_000_000_000


def _child(account: str | None = "DU111") -> BrokerBlock:
    return BrokerBlock(
        identity="PAPER_VERIFIED",
        submission_capability="PAPER_ORDERS_ENABLED",
        effective_posture="PAPER_EXECUTION",
        connection_state="connected",
        connection_epoch=1,
        connected_account=account,
        port_class="paper_port",
        observation_at_ms=_NOW_MS - 1_000,
        probe_completed_at_ms=_NOW_MS - 1_000,
        reconnect_attempt=0,
    )


def _data_plane(
    *,
    available: bool = True,
    connected: bool = True,
    account: str | None = "DU111",
    mode: str | None = "paper",
) -> BrokerRuntimeSnapshot:
    return BrokerRuntimeSnapshot(
        client_available=available,
        connected=connected,
        configured_mode=mode,  # type: ignore[arg-type]
        readonly=False,
        port=7497,
        connected_account=account,
        connection_state=None,
    )


def test_consistent_when_both_accounts_match() -> None:
    result = evaluate_broker_observation_consistency(
        child=_child("DU111"),
        data_plane=_data_plane(account="DU111"),
        child_configured_mode="paper",
        now_ms=_NOW_MS,
    )

    assert result.verdict == "CONSISTENT"
    assert result.reason_codes == ["ACCOUNTS_MATCH"]
    assert result.child_account == "DU111"
    assert result.data_plane_account == "DU111"
    assert result.compared_at_ms == _NOW_MS


def test_conflicting_when_accounts_differ() -> None:
    result = evaluate_broker_observation_consistency(
        child=_child("DU111"),
        data_plane=_data_plane(account="DU222"),
        child_configured_mode="paper",
        now_ms=_NOW_MS,
    )

    assert result.verdict == "CONFLICTING"
    assert result.reason_codes == ["ACCOUNTS_DIVERGE"]
    assert result.child_account == "DU111"
    assert result.data_plane_account == "DU222"


def test_unknown_when_child_block_missing() -> None:
    result = evaluate_broker_observation_consistency(
        child=None,
        data_plane=_data_plane(account="DU111"),
        child_configured_mode=None,
        now_ms=_NOW_MS,
    )

    assert result.verdict == "UNKNOWN"
    assert "CHILD_OBSERVATION_MISSING" in result.reason_codes
    assert result.child_account is None


def test_unknown_when_child_account_empty() -> None:
    result = evaluate_broker_observation_consistency(
        child=_child(None),
        data_plane=_data_plane(account="DU111"),
        child_configured_mode=None,
        now_ms=_NOW_MS,
    )

    assert result.verdict == "UNKNOWN"
    assert "CHILD_OBSERVATION_MISSING" in result.reason_codes


def test_unknown_when_data_plane_unavailable() -> None:
    result = evaluate_broker_observation_consistency(
        child=_child("DU111"),
        data_plane=_data_plane(available=False, connected=False, account=None),
        child_configured_mode=None,
        now_ms=_NOW_MS,
    )

    assert result.verdict == "UNKNOWN"
    assert "DATA_PLANE_OBSERVATION_MISSING" in result.reason_codes


def test_unknown_when_data_plane_disconnected() -> None:
    result = evaluate_broker_observation_consistency(
        child=_child("DU111"),
        data_plane=_data_plane(available=True, connected=False, account=None),
        child_configured_mode=None,
        now_ms=_NOW_MS,
    )

    assert result.verdict == "UNKNOWN"
    assert "DATA_PLANE_DISCONNECTED" in result.reason_codes


def test_not_comparable_when_modes_differ() -> None:
    # Mode mismatch trumps any account comparison — apples vs oranges.
    result = evaluate_broker_observation_consistency(
        child=_child("DU111"),
        data_plane=_data_plane(account="U999999", mode="live"),
        child_configured_mode="paper",
        now_ms=_NOW_MS,
    )

    assert result.verdict == "NOT_COMPARABLE"
    assert result.reason_codes == ["CONFIGURED_MODES_DIVERGE"]


def test_falls_back_to_account_comparison_when_mode_unknown() -> None:
    # When the child's configured mode isn't supplied, the mode-mismatch
    # check is skipped; the normal account comparison runs.
    result = evaluate_broker_observation_consistency(
        child=_child("DU111"),
        data_plane=_data_plane(account="DU111", mode="paper"),
        child_configured_mode=None,
        now_ms=_NOW_MS,
    )

    assert result.verdict == "CONSISTENT"


@pytest.mark.parametrize(
    "code",
    [
        "ACCOUNTS_MATCH",
        "ACCOUNTS_DIVERGE",
        "CHILD_OBSERVATION_MISSING",
        "DATA_PLANE_OBSERVATION_MISSING",
        "DATA_PLANE_DISCONNECTED",
        "CONFIGURED_MODES_DIVERGE",
    ],
)
def test_documented_code_in_vocabulary(code: str) -> None:
    assert code in REASON_CODES
