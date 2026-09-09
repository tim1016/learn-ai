"""The supervised arming ceremony: observe, plan, apply, disarm (ADR 0059 D3)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_INPUTS_CHANGED,
    LIVE_ARMING_INSTANCE_UNSEALED,
    LIVE_ARMING_NOT_ARMED,
    LIVE_ARMING_PLAN_EXPIRED,
    LIVE_ARMING_TOKEN_INVALID,
    LIVE_ARMING_TTL_INVALID,
    LIVE_ENVELOPE_MISSING,
    LIVE_SHADOW_INCOMPLETE,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
)
from app.broker.alpaca.clerk.live_arming_ceremony import (
    ArmingInputs,
    ArmingObserver,
    LiveArmingPlan,
    account_arming_statuses,
    apply_arming,
    custody_account_ids_for,
    disarm,
    instance_seal_hashes,
    live_account_id_for,
    observe_arming_inputs,
    plan_arming,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMED_AT_MS,
    ARMING_SID,
    activate_shadow_fence,
    arming_ready,
    live_settings,
    paper_settings,
    record_sealed_binding,
    seal_receipt,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    LIVE_ACCT,
    SHADOW_ACCT,
    TEST_ENVELOPE_VALUES,
)

TTL_MS = 120_000


class _Clock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """The Clerk artifacts root and the runner ``live_state`` root, kept apart."""
    return tmp_path / "clerk", tmp_path / "runner"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _inputs(**overrides: object) -> ArmingInputs:
    values: dict[str, object] = {
        "live_account_id": LIVE_ACCT,
        "strategy_instance_id": ARMING_SID,
        "seal_hash": "a" * 64,
        "configured_signal_hash": "b" * 64,
        "shadow_receipt_sha256": "c" * 64,
        "envelope": TEST_ENVELOPE_VALUES,
        "max_sessions": TEST_ENVELOPE_VALUES.arming_max_sessions,
    }
    values.update(overrides)
    return ArmingInputs(**values)  # type: ignore[arg-type]


def _observer(inputs: ArmingInputs) -> ArmingObserver:
    def _observe(**_kwargs: object) -> ArmingInputs:
        return inputs

    return _observe


def _plan_with(inputs: ArmingInputs, artifacts_root: Path, *, now_ms: int = ARMED_AT_MS) -> LiveArmingPlan:
    return plan_arming(
        strategy_instance_id=inputs.strategy_instance_id,
        artifacts_root=artifacts_root,
        live_state_root=artifacts_root,
        settings=live_settings(),
        clock=_Clock(now_ms),
        observe=_observer(inputs),
    )


def test_the_two_custody_ids_of_one_live_account_are_both_admissible() -> None:
    """Shadow custody seals ``shadow:<id>``; slice 7's real_live custody seals ``<id>``."""
    assert custody_account_ids_for(LIVE_ACCT) == frozenset({LIVE_ACCT, SHADOW_ACCT})


def test_the_observer_reads_the_four_inputs_off_disk(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    seal = arming_ready(artifacts_root, live_state_root)

    inputs = observe_arming_inputs(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
    )

    assert inputs.live_account_id == LIVE_ACCT
    # R2: arming binds to the whole sealed program, not the signal-only hash.
    assert inputs.seal_hash == seal.bot_configuration_hash
    assert inputs.configured_signal_hash == seal.configured_signal_hash
    assert inputs.seal_hash != inputs.configured_signal_hash
    assert len(inputs.shadow_receipt_sha256) == 64
    assert inputs.envelope == TEST_ENVELOPE_VALUES
    assert inputs.max_sessions == TEST_ENVELOPE_VALUES.arming_max_sessions


def test_a_paper_account_is_never_armed(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    with pytest.raises(LiveArmingRefused) as caught:
        observe_arming_inputs(
            strategy_instance_id=ARMING_SID,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=paper_settings(),
        )

    assert caught.value.reason_code == LIVE_ENVELOPE_MISSING


def test_an_incomplete_live_envelope_refuses_by_the_same_code(roots: tuple[Path, Path]) -> None:
    """``model_copy`` bypasses the settings validator, which is the only way to
    reach this branch -- ``AlpacaSettings`` itself refuses to construct a live
    mode with a missing value, so this is the defence behind that door."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    incomplete = live_settings().model_copy(update={"live_loss_usd": None})

    with pytest.raises(LiveArmingRefused) as caught:
        observe_arming_inputs(
            strategy_instance_id=ARMING_SID,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=incomplete,
        )

    assert caught.value.reason_code == LIVE_ENVELOPE_MISSING
    assert "live_loss_usd" in str(caught.value)


def test_no_shadow_activation_proof_refuses_before_any_binding_is_read(roots: tuple[Path, Path]) -> None:
    artifacts_root, _live_state_root = roots
    artifacts_root.mkdir(parents=True)

    with pytest.raises(LiveArmingRefused) as caught:
        live_account_id_for(artifacts_root)

    assert caught.value.reason_code == LIVE_ARMING_INSTANCE_UNSEALED
    assert "manage_alpaca_shadow activate" in str(caught.value)


def test_two_shadowed_accounts_refuse_rather_than_choosing_one(roots: tuple[Path, Path]) -> None:
    artifacts_root, _live_state_root = roots
    activate_shadow_fence(artifacts_root, live_account_id=LIVE_ACCT)
    activate_shadow_fence(artifacts_root, live_account_id="9LIVE0002")

    with pytest.raises(LiveArmingRefused) as caught:
        live_account_id_for(artifacts_root)

    assert caught.value.reason_code == LIVE_ARMING_INSTANCE_UNSEALED
    assert "more than one shadowed live account" in str(caught.value)


def test_an_unsealed_or_foreign_binding_is_not_an_armable_instance(roots: tuple[Path, Path]) -> None:
    from app.services.bot_binding_repository import (
        BrokerBotBinding,
        alpaca_v1_action_plan,
        live_state_binding_repository,
    )

    artifacts_root, live_state_root = roots
    activate_shadow_fence(artifacts_root)
    # A legacy binding with no v2 seal: skipped, never refused, so it cannot
    # stop a sealed sibling from arming.
    live_state_binding_repository(live_state_root).record_launch(
        BrokerBotBinding(
            strategy_instance_id="legacy",
            broker="alpaca",
            symbol="SPY",
            mode="trade",
            action_plan=alpaca_v1_action_plan("SPY"),
            run_id="legacy-run-1",
            created_at_ms=1_757_000_000_000,
        ),
        launch_reason="deploy",
    )
    record_sealed_binding(live_state_root, strategy_instance_id="elsewhere", sealed_account_id="PA-OTHER")
    sealed = record_sealed_binding(live_state_root, strategy_instance_id=ARMING_SID)

    seals = instance_seal_hashes(live_account_id=LIVE_ACCT, live_state_root=live_state_root)

    assert set(seals) == {ARMING_SID}
    assert seals[ARMING_SID].seal_hash == sealed.bot_configuration_hash

    with pytest.raises(LiveArmingRefused) as caught:
        observe_arming_inputs(
            strategy_instance_id="legacy",
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
        )
    assert caught.value.reason_code == LIVE_ARMING_INSTANCE_UNSEALED


def test_no_current_shadow_receipt_is_the_adrs_own_refusal(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    activate_shadow_fence(artifacts_root)
    record_sealed_binding(live_state_root)
    # A receipt for a different configured signal is not this seal's proof.
    seal_receipt(artifacts_root, configured_signal_hash="f" * 64)

    with pytest.raises(LiveArmingRefused) as caught:
        observe_arming_inputs(
            strategy_instance_id=ARMING_SID,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
        )

    assert caught.value.reason_code == LIVE_SHADOW_INCOMPLETE


def test_plan_writes_nothing_and_its_two_ids_are_its_own_content_hash(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    before = _snapshot(artifacts_root)

    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        confirmation_ttl_ms=TTL_MS,
        clock=_Clock(ARMED_AT_MS),
    )

    assert _snapshot(artifacts_root) == before
    assert plan.schema_version == 1
    assert plan.plan_id == plan.confirmation_token and len(plan.plan_id) == 64
    assert (plan.created_at_ms, plan.expires_at_ms) == (ARMED_AT_MS, ARMED_AT_MS + TTL_MS)
    assert plan.live_account_id == LIVE_ACCT
    assert plan.envelope_sha256 == TEST_ENVELOPE_VALUES.sha
    assert plan.envelope_values == TEST_ENVELOPE_VALUES.to_mapping()
    assert plan.max_sessions == TEST_ENVELOPE_VALUES.arming_max_sessions


@pytest.mark.parametrize("ttl", [0, -1, 300_001])
def test_a_confirmation_window_outside_the_bound_is_refused(roots: tuple[Path, Path], ttl: int) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    with pytest.raises(LiveArmingRefused) as caught:
        plan_arming(
            strategy_instance_id=ARMING_SID,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            confirmation_ttl_ms=ttl,
            clock=_Clock(ARMED_AT_MS),
        )

    assert caught.value.reason_code == LIVE_ARMING_TTL_INVALID


def test_apply_arms_the_instance_and_appends_exactly_one_sealed_record(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    seal = arming_ready(artifacts_root, live_state_root)
    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )

    record = apply_arming(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS + 1_000),
    )

    assert isinstance(record, LiveArmingRecord)
    assert record.armed_at_ms == ARMED_AT_MS + 1_000
    assert record.seal_hash == seal.bot_configuration_hash
    assert record.envelope_sha256 == TEST_ENVELOPE_VALUES.sha
    ledger = LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT)
    assert ledger.records() == (record,)
    assert ledger.sealed_envelope() == TEST_ENVELOPE_VALUES


def test_apply_refuses_a_token_that_is_not_the_plans(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = _plan_with(_inputs(), artifacts_root)

    with pytest.raises(LiveArmingRefused) as caught:
        apply_arming(
            plan=plan,
            confirmation_token="0" * 64,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS),
            observe=_observer(_inputs()),
        )

    assert caught.value.reason_code == LIVE_ARMING_TOKEN_INVALID
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_apply_refuses_a_plan_whose_content_was_edited(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = _plan_with(_inputs(), artifacts_root)
    forged = replace(plan, max_sessions=999)

    with pytest.raises(LiveArmingRefused) as caught:
        apply_arming(
            plan=forged,
            confirmation_token=forged.confirmation_token,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS),
            observe=_observer(_inputs()),
        )

    assert caught.value.reason_code == LIVE_ARMING_TOKEN_INVALID
    assert "content hash does not verify" in str(caught.value)


def test_apply_refuses_once_the_confirmation_window_has_closed(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        confirmation_ttl_ms=10,
        clock=_Clock(ARMED_AT_MS),
    )

    # The last admissible millisecond still applies.
    on_the_edge = apply_arming(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS + 10),
    )
    assert on_the_edge.armed_at_ms == ARMED_AT_MS + 10

    with pytest.raises(LiveArmingRefused) as caught:
        apply_arming(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS + 11),
        )
    assert caught.value.reason_code == LIVE_ARMING_PLAN_EXPIRED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("live_account_id", "9LIVE0002"),
        ("strategy_instance_id", "someone-else"),
        ("seal_hash", "d" * 64),
        ("configured_signal_hash", "d" * 64),
        ("shadow_receipt_sha256", "d" * 64),
        ("max_sessions", 5),
    ],
)
def test_apply_refuses_every_input_that_drifted_between_plan_and_apply(
    roots: tuple[Path, Path], field: str, value: object
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    planned = _inputs()
    plan = _plan_with(planned, artifacts_root)

    with pytest.raises(LiveArmingRefused) as caught:
        apply_arming(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS),
            observe=_observer(replace(planned, **{field: value})),
        )

    assert caught.value.reason_code == LIVE_ARMING_INPUTS_CHANGED
    assert field in str(caught.value)
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_apply_refuses_an_envelope_that_changed_between_plan_and_apply(roots: tuple[Path, Path]) -> None:
    """The envelope drifts by its sha, not by its seven-field object identity."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    planned = _inputs()
    plan = _plan_with(planned, artifacts_root)

    with pytest.raises(LiveArmingRefused) as caught:
        apply_arming(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS),
            observe=_observer(replace(planned, envelope=replace(TEST_ENVELOPE_VALUES, loss_usd=4_000.0))),
        )

    assert caught.value.reason_code == LIVE_ARMING_INPUTS_CHANGED
    assert "envelope_sha256" in str(caught.value)


def test_re_arming_while_armed_supersedes_with_no_already_armed_refusal(roots: tuple[Path, Path]) -> None:
    """R7: renewal before lapse is the same ceremony, run again."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    for offset in (0, 60_000):
        plan = plan_arming(
            strategy_instance_id=ARMING_SID,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS + offset),
        )
        apply_arming(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS + offset),
        )

    ledger = LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT)
    assert len(ledger.records()) == 2
    latest = ledger.latest(ARMING_SID)
    assert isinstance(latest, LiveArmingRecord) and latest.armed_at_ms == ARMED_AT_MS + 60_000


def test_disarm_revokes_the_latest_record_and_needs_no_confirmation(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )
    armed = apply_arming(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )

    revocation = disarm(
        strategy_instance_id=ARMING_SID, artifacts_root=artifacts_root, clock=_Clock(ARMED_AT_MS + 5_000)
    )

    assert isinstance(revocation, LiveDisarmRecord)
    assert revocation.revokes_record_sha256 == armed.record_sha256
    assert revocation.disarmed_at_ms == ARMED_AT_MS + 5_000
    statuses = account_arming_statuses(
        live_account_id=LIVE_ACCT,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        configured_envelope=TEST_ENVELOPE_VALUES,
        now_ms=ARMED_AT_MS + 5_000,
    )
    assert statuses[ARMING_SID].state == "disarmed"
    assert statuses[ARMING_SID].reason_code == "LIVE_ARMING_REVOKED"


def test_disarm_with_nothing_to_revoke_is_refused_rather_than_written(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    with pytest.raises(LiveArmingRefused) as caught:
        disarm(strategy_instance_id=ARMING_SID, artifacts_root=artifacts_root, clock=_Clock(ARMED_AT_MS))

    assert caught.value.reason_code == LIVE_ARMING_NOT_ARMED
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_account_statuses_answer_every_instance_with_a_row_and_named_ones_besides(
    roots: tuple[Path, Path],
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )
    apply_arming(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )

    every = account_arming_statuses(
        live_account_id=LIVE_ACCT,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        configured_envelope=TEST_ENVELOPE_VALUES,
        now_ms=ARMED_AT_MS,
    )
    assert set(every) == {ARMING_SID}
    assert every[ARMING_SID].state == "armed"
    assert (every[ARMING_SID].sessions_used, every[ARMING_SID].sessions_remaining) == (1, 19)

    named = account_arming_statuses(
        live_account_id=LIVE_ACCT,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        configured_envelope=TEST_ENVELOPE_VALUES,
        now_ms=ARMED_AT_MS,
        strategy_instance_ids=["never-armed"],
    )
    assert named["never-armed"].state == "unarmed"


def test_a_size_change_alone_disarms_because_arming_binds_the_whole_seal(
    roots: tuple[Path, Path],
) -> None:
    """R2's whole point, end to end: quantity is inside ``bot_configuration_hash``."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )
    apply_arming(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )
    resized = record_sealed_binding(
        live_state_root / "resized", strategy_instance_id=ARMING_SID, quantity=7
    )
    assert resized.bot_configuration_hash != plan.seal_hash

    statuses = account_arming_statuses(
        live_account_id=LIVE_ACCT,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root / "resized",
        configured_envelope=TEST_ENVELOPE_VALUES,
        now_ms=ARMED_AT_MS,
    )

    assert statuses[ARMING_SID].state == "disarmed"
    assert statuses[ARMING_SID].reason_code == "LIVE_ARMING_SEAL_CHANGED"
