"""Slice 1A — ``live_config.action`` is hashed into ``run_id``.

The action-plan ledger key is on the allow-list (#594 / PRD #593) and the
deploy boundary accepts it; this test pins the hashing invariant the
issue's acceptance criteria depend on:

  * identical inputs (including identical ``action``) → identical ``run_id``
    (idempotent-deploy contract holds — same as the sizing precedent),
  * a different ``action`` with otherwise identical inputs →
    different ``run_id`` (so the operator's declared intent really is
    part of run identity, not silently dropped).

Prior art: ``app.engine.live.run_ledger.compute_run_id`` (the sizing key
is hashed the same way).
"""

from __future__ import annotations

from app.engine.live.run_ledger import compute_run_id


_BASE_IDENTITY: dict[str, object] = {
    "code_sha": "abc",
    "strategy_spec_sha256": "spec-sha",
    "qc_audit_copy_sha256": "audit-sha",
    "qc_cloud_backtest_id": "bt-1",
    "account_id": "DU111",
    "start_date_ms": 0,
}


def _live_config(action: dict | None) -> dict:
    cfg: dict[str, object] = {"sizing": {"kind": "FixedShares", "value": 1}}
    if action is not None:
        cfg["action"] = action
    return cfg


def test_identical_action_plans_yield_identical_run_ids() -> None:
    run_id_a = compute_run_id(
        **_BASE_IDENTITY,
        live_config=_live_config({"on_enter": [], "on_exit": []}),
    )
    run_id_b = compute_run_id(
        **_BASE_IDENTITY,
        live_config=_live_config({"on_enter": [], "on_exit": []}),
    )

    assert run_id_a == run_id_b


def test_different_action_plans_yield_different_run_ids() -> None:
    """Sentinel divergence: a synthetic ``on_enter`` entry (shape lands
    in #595) must already produce a different hash today so the
    idempotent-redeploy contract really attests to the *declared* plan
    rather than collapsing every plan to the same identity."""

    base_action = {"on_enter": [], "on_exit": []}
    differing_action = {"on_enter": [{"sentinel": "future-leg"}], "on_exit": []}

    run_id_base = compute_run_id(
        **_BASE_IDENTITY, live_config=_live_config(base_action)
    )
    run_id_diff = compute_run_id(
        **_BASE_IDENTITY, live_config=_live_config(differing_action)
    )

    assert run_id_base != run_id_diff


def test_empty_action_plan_changes_run_id_vs_absent_action() -> None:
    """Adding ``action: {on_enter: [], on_exit: []}`` to a previously
    action-less ledger changes ``run_id``. This guards against a future
    change where the empty plan accidentally serialized identically to
    its absence — the cockpit label ("Declared action plan — not active
    until Slice 4") would then mis-attest to runs that pre-date the
    field."""

    run_id_without = compute_run_id(
        **_BASE_IDENTITY, live_config=_live_config(None)
    )
    run_id_with_empty = compute_run_id(
        **_BASE_IDENTITY,
        live_config=_live_config({"on_enter": [], "on_exit": []}),
    )

    assert run_id_without != run_id_with_empty
