"""Real graduation/restart proves reviewed risk is initialized before execution."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.active_authority import select_active_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.activation import ActivationStore
from app.broker.alpaca.clerk.sqlite.cutover import BrokerCutoverEvidence, CutoverRefused, decode_cutover_plan
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.risk_admission import require_current_risk_admission
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, LossHoldCause
from app.services import alpaca_live_graduation as graduation
from app.utils.timestamps import now_ms_utc
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, SHADOW_ACCT, TEST_ENVELOPE_VALUES, _LiveBroker


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    now = now_ms_utc()
    runner = tmp_path / "runner"
    (runner / "live_state").mkdir(parents=True)
    source = ClerkSqliteRepository.initialize(account_id=SHADOW_ACCT, artifacts_root=tmp_path, clock=lambda: now)
    append_risk_policy(source, policy=AccountRiskPolicy(1, .03, 300, "profile", 1, "owner", now), expected_revision=0)
    runtime = ActiveClerkRuntime(authority_kind="shadow", account_id=SHADOW_ACCT, account_authority_kind="shadow", _sqlite_repository=source)
    configured = SimpleNamespace(mode="live", live_loss_fraction=.9, live_loss_usd=9000,
        live_xh_entry_bps=10, live_xh_exit_bps=10, clerk_dir=tmp_path)
    selection = SimpleNamespace(effective_account_id=LIVE_ACCT, effective_profile_id="profile", effective_revision=1, selection_generation=2)
    binding = SimpleNamespace(settings=configured, profile_id="profile", revision=1)
    service = SimpleNamespace(selection_handover=nullcontext, selection=lambda: selection, owner=lambda: SimpleNamespace(owner_id="owner"))
    monkeypatch.setattr(graduation, "resolved_alpaca_settings", lambda: configured)
    monkeypatch.setattr(graduation, "get_active_alpaca_binding", lambda: binding)
    monkeypatch.setattr(graduation, "get_active_clerk_runtime", lambda: runtime)
    monkeypatch.setattr(graduation, "get_broker_configuration_service", lambda: service)
    monkeypatch.setattr(graduation, "live_artifacts_root", lambda: runner)
    monkeypatch.setattr(graduation.settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    monkeypatch.setattr(graduation.fleet_settings, "WORKER_SERVICE", "live-worker")
    captures = []

    async def capture(self, account_id):
        captures.append(account_id)
        return BrokerCutoverEvidence(account_id, "live", now_ms_utc(), f"fresh-proof-{len(captures)}", {}, ())

    monkeypatch.setattr(graduation.AlpacaLiveGraduationService, "_capture_broker_evidence", capture)
    yield SimpleNamespace(source=source, selection=selection, service=graduation.AlpacaLiveGraduationService(), root=tmp_path, now=now, captures=captures)
    source.close()


async def _boot(world):
    return await select_active_clerk_runtime(read=_LiveBroker(now_ms=world.now), trade=_LiveBroker(now_ms=world.now),
        artifacts_root=world.root, live_envelope_values=TEST_ENVELOPE_VALUES,
        repository_opener=lambda account, root: ClerkSqliteRepository.open(account_id=account, artifacts_root=root, clock=lambda: world.now),
        control_unauthenticated=False)


async def test_review_uses_effective_policy_and_restart_initializes_it_without_shadow_hold(world):
    raise_account_hold(world.source, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, evidence_refs=["shadow-loss"],
        cause_facts=LossHoldCause(world.now, -400, 300, 10_000, world.now, 1).to_mapping())
    view = await world.service.prepare(LIVE_ACCT)
    assert view.daily_loss_fraction == .03 and view.daily_loss_usd == 300
    plan, _ = world.service._read_persisted_plan(LIVE_ACCT, view.plan_id)
    assert plan.schema_version == 4 and decode_cutover_plan(asdict(plan)) == plan
    await world.service.apply(LIVE_ACCT, plan_id=view.plan_id, confirmation_token=view.confirmation_token)
    assert len(world.captures) == 2  # Fresh proof/timestamp is accepted when actual state is unchanged.
    # Simulate a process death just after activation: target policy is not yet
    # materialized, so no runtime could have granted exposure in that interval.
    repo = ClerkSqliteRepository.open(account_id=LIVE_ACCT, artifacts_root=world.root)
    assert repo.account_risk_policy() is None
    repo.close()
    runtime = await _boot(world)
    try:
        assert runtime.authority_kind == "sqlite", runtime.startup_failure
        target = runtime.sqlite_repository
        policy = target.account_risk_policy()
        assert policy.loss_fraction == .03 and policy.loss_usd == 300 and policy.revision == 1
        assert target._conn.execute("SELECT COUNT(*) FROM deployment_budgets").fetchone()[0] == 0
        assert target.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None) is None
        assert world.source.account_risk_policy().loss_usd == 300
        assert world.source.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None) is not None
        record_fee_evidence(target, [], checked_at_ms=world.now, history_complete=True)
        assert await runtime.envelope_sync.tick() == "observed"
        assert require_current_risk_admission(target, envelope=runtime.envelope_sync.envelope, now_ms=world.now).risk_revision == 1
        append_risk_policy(target, policy=replace(policy, revision=2, loss_usd=400), expected_revision=1)
    finally:
        await runtime.close()
    reopened = await _boot(world)
    try:
        assert reopened.sqlite_repository.account_risk_policy().loss_usd == 400  # never reset a later Apply
        rows = [r for r in reopened.sqlite_repository.custody_transitions() if r["transition_kind"] == "ACCOUNT_RISK_LIMITS_APPLIED"]
        assert len(rows) == 2
    finally:
        await reopened.close()


@pytest.mark.parametrize("change", ["policy", "selection"])
async def test_changed_risk_or_selection_refuses_before_activation(world, change):
    view = await world.service.prepare(LIVE_ACCT)
    if change == "policy":
        append_risk_policy(world.source, policy=replace(world.source.account_risk_policy(), revision=2, loss_usd=400), expected_revision=1)
    else:
        world.selection.selection_generation += 1
    with pytest.raises(graduation.LiveGraduationRefused, match="changed"):
        await world.service.apply(LIVE_ACCT, plan_id=view.plan_id, confirmation_token=view.confirmation_token)
    assert ActivationStore(world.root / "accounts" / "alpaca").latest(LIVE_ACCT) is None


async def test_forged_review_cannot_keep_the_old_confirmation(world):
    view = await world.service.prepare(LIVE_ACCT)
    plan, backup = world.service._read_persisted_plan(LIVE_ACCT, view.plan_id)
    changed = replace(plan, graduation_risk=replace(plan.graduation_risk, source_policy=replace(plan.graduation_risk.source_policy, loss_usd=999)))
    with pytest.raises(CutoverRefused, match="content hash"):
        world.service._apply_reviewed_plan(changed, backup, view.confirmation_token, plan.broker_evidence)


async def test_policy_initialization_failure_prevents_live_runtime_installation(world, monkeypatch):
    view = await world.service.prepare(LIVE_ACCT)
    await world.service.apply(LIVE_ACCT, plan_id=view.plan_id, confirmation_token=view.confirmation_token)
    from app.broker.alpaca.clerk import live_authority

    def failed(*args, **kwargs):
        raise OSError("simulated policy durability failure")

    with monkeypatch.context() as context:
        context.setattr(live_authority, "initialize_reviewed_live_risk", failed)
        refused = await _boot(world)
        assert refused.authority_kind == "unavailable" and refused.clerk is None
    recovered = await _boot(world)
    try:
        assert recovered.authority_kind == "sqlite"
        assert recovered.sqlite_repository.account_risk_policy().loss_usd == 300
    finally:
        await recovered.close()
