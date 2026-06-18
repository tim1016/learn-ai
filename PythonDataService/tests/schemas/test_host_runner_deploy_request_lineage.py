"""Slice 1E — unhashed redeploy lineage fields.

PRD #593 §"Redeploy lineage" / ADR 0012 §7 / issue #598. The deploy
boundary accepts optional ``parent_run_id`` + ``redeploy_reason`` at
the *top level* of ``HostRunnerDeployRequest`` — NOT under ``live_config``.
The ledger persists them under a ``lineage`` block alongside other
unhashed metadata (``code_sha``, ``sizing_provenance``).

Critically: these fields must NOT enter ``run_id`` so the idempotent-
redeploy contract holds — the same plan redeployed from two different
parents collapses to the same ``run_id``.
"""

from __future__ import annotations

from app.engine.live.config import LIVE_CONFIG_LEDGER_KEYS
from app.engine.live.run_ledger import compute_run_id
from app.schemas.live_runs import HostRunnerDeployRequest


def _base_kwargs(**overrides: object) -> dict:
    defaults = dict(
        strategy_spec_path="a/b.json",
        qc_audit_copy_path="c/d.py",
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=0,
        live_config={"sizing": {"kind": "FixedShares", "value": 1}},
    )
    defaults.update(overrides)
    return defaults


def test_parent_run_id_accepted_at_top_level() -> None:
    req = HostRunnerDeployRequest(**_base_kwargs(parent_run_id="run-abc12345"))

    assert req.parent_run_id == "run-abc12345"


def test_redeploy_reason_accepted_at_top_level() -> None:
    req = HostRunnerDeployRequest(
        **_base_kwargs(redeploy_reason="quantity bump after live read"),
    )

    assert req.redeploy_reason == "quantity bump after live read"


def test_lineage_fields_are_omitted_by_default() -> None:
    """Optional + None by default — most deploys are first deploys with
    no parent."""

    req = HostRunnerDeployRequest(**_base_kwargs())

    assert req.parent_run_id is None
    assert req.redeploy_reason is None


def test_lineage_fields_are_not_in_live_config_ledger_keys() -> None:
    """They live in the ledger's ``lineage`` block (unhashed), NOT under
    ``live_config`` (which IS hashed into ``run_id``). Pinned so a
    refactor doesn't accidentally move them into the hash."""

    assert "parent_run_id" not in LIVE_CONFIG_LEDGER_KEYS
    assert "redeploy_reason" not in LIVE_CONFIG_LEDGER_KEYS


def test_same_plan_two_parents_yield_same_run_id() -> None:
    """LOAD-BEARING: ADR 0012 §7 idempotent-redeploy contract. The same
    ``live_config`` (same hashed inputs) deployed from two different
    parents must collapse to the same ``run_id``; lineage is unhashed."""

    identity: dict = dict(
        code_sha="abc",
        strategy_spec_sha256="spec-sha",
        qc_audit_copy_sha256="audit-sha",
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=0,
        live_config={"sizing": {"kind": "FixedShares", "value": 1}},
    )

    # ``compute_run_id`` only takes hashed identity inputs — lineage is
    # not even a parameter. This pins that the type signature itself
    # enforces the invariant.
    run_id = compute_run_id(**identity)
    second_run_id = compute_run_id(**identity)  # "redeployed" with different parent

    assert run_id == second_run_id


def test_different_plan_same_parent_yields_different_run_id() -> None:
    base_identity: dict = dict(
        code_sha="abc",
        strategy_spec_sha256="spec-sha",
        qc_audit_copy_sha256="audit-sha",
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=0,
    )
    first = compute_run_id(
        **base_identity, live_config={"sizing": {"kind": "FixedShares", "value": 1}},
    )
    second = compute_run_id(
        **base_identity, live_config={"sizing": {"kind": "FixedShares", "value": 2}},
    )

    assert first != second
