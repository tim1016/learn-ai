"""Trader money review uses custody facts and immutable consent, not UI math."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.schemas.broker_bots import AlpacaPaperDeployRequest
from app.schemas.deployment_budget import (
    BudgetDeployCommandReceipt,
    DeploymentBudgetInput,
    DeploymentBudgetPreview,
    DeploymentBudgetView,
)
from app.schemas.exit_terms import ExitTermsInput
from app.schemas.market_liveness import MarketStatusSnapshot, MarketStatusSource, TopOfBookQuote
from app.services import market_liveness
from app.services.broker_v2_panel import budget_deploy
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock


@pytest.mark.parametrize(("model", "field", "payload"), [
    (DeploymentBudgetPreview, "observed_at_ms", dict(state="ready", detail="Ready", world="real_paper", custody_account_id="PAPER")),
    (DeploymentBudgetView, "observed_at_ms", dict(state="ready", headline="Ready", detail="Ready", world="real_paper", strategy_instance_id="bot")),
    (BudgetDeployCommandReceipt, "recorded_at_ms", dict(
        status="pending", outcome="pending", receipt_id="receipt", command_id="command", strategy_instance_id="bot",
        run_id="run", account_id="PAPER", world="real_paper", committed_usd="100.00", message="Pending",
        explanation="Pending", next_action="Refresh", panel_path="/panel",
    )),
])
def test_budget_receipts_reject_out_of_domain_timestamps(model: type[BaseModel], field: str, payload: dict) -> None:
    assert getattr(model(**payload, **{field: MAX_TIMESTAMP_MS}), field) == MAX_TIMESTAMP_MS
    with pytest.raises(ValidationError, match=field):
        model(**payload, **{field: MAX_TIMESTAMP_MS + 1})


def _publish_book(*asks: tuple[str, float]) -> None:
    """Publish one fresh IBKR book to the real process store, as the status source does."""
    market_liveness.get_market_liveness_store().apply_status_snapshot(MarketStatusSnapshot(
        source=MarketStatusSource.IBKR, connected=True, observed_at_ms=NOON, connection_changed_at_ms=NOON,
        symbol_statuses=(), quotes=tuple(
            TopOfBookQuote(symbol=symbol, bid=ask, ask=ask, source="ibkr.market_data.status", observed_at_ms=NOON)
            for symbol, ask in asks
        ),
    ), now_ms=NOON)


def _request(symbol: str = "SPY", **updates) -> AlpacaPaperDeployRequest:
    return AlpacaPaperDeployRequest(
        strategy_instance_id="review-a", strategy_key="deployment_validation", symbol=symbol,
        exit_terms=ExitTermsInput(band_multiple=2, spread_cap_bps=100, exit_allowance_bps=5),
        **updates,
    )


@pytest.fixture
def authority(tmp_path, monkeypatch):
    repo = ClerkSqliteRepository.initialize(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON))
    policy = AccountRiskPolicy(1, .1, 100, "profile", 1, "owner", NOON)
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
    append_risk_policy(repo, policy=policy, expected_revision=0)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    observation = AccountObservation(observed_at_ms=NOON, broker_cash_usd=1000, cash_available_usd=1000,
        last_equity_usd=1000, unrealized_pl_usd=0, position_count=0, risk_revision=1, equity_usd=1000,
        risk_cash_flow_evidence_complete=True, risk_cash_flow_window_start_ms=day_pnl_window_start_ms(NOON),
        risk_equity_window_start_ms=day_pnl_window_start_ms(NOON))
    snapshot = SimpleNamespace(observation=observation, policy=policy, hold=None)
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    gate.publish(observation)
    runtime = SimpleNamespace(selected_account_id=repo.account_id, account_authority_kind="real_paper", sqlite_repository=repo, envelope_sync=SimpleNamespace(envelope=gate, risk_snapshot=lambda: snapshot))
    monkeypatch.setattr(budget_deploy, "get_active_clerk_runtime", lambda: runtime)
    monkeypatch.setattr(budget_deploy, "get_clerk_runtime", lambda account_id: None)
    monkeypatch.setattr(budget_deploy, "get_broker_configuration_service", lambda: SimpleNamespace(owner=lambda: SimpleNamespace(owner_id="server-owner")))
    market_liveness.reset_market_liveness_store_for_testing()
    _publish_book(("SPY", 100.001))
    yield repo, runtime, snapshot
    market_liveness.reset_market_liveness_store_for_testing()
    repo.close()


def test_preview_shortcuts_are_server_authored_and_round_at_boundary(authority) -> None:
    preview = budget_deploy.preview_budget("BUDGET-PAPER", _request(), resolved_parameters={})
    assert preview.state == "ready"
    assert preview.minimum_budget_usd == "100.02"
    assert {row.key: row.amount_usd for row in preview.shortcuts} == {"quarter": "250.00", "half": "500.00", "all": "1000.00", "position_headroom": "120.02"}
    assert preview.review_token is None


def test_confirmation_binds_amount_effective_parameters_and_risk(authority) -> None:
    request = _request(budget=DeploymentBudgetInput(amount_usd="500", risk_revision=1))
    preview = budget_deploy.preview_budget("BUDGET-PAPER", request, resolved_parameters={"period": 5})
    reviewed = request.model_copy(update={"budget": request.budget.model_copy(update={"review_token": preview.review_token})})
    assert budget_deploy.resolve_consent("BUDGET-PAPER", reviewed, resolved_parameters={"period": 5}).actor == "server-owner"
    with pytest.raises(BudgetUnavailable):
        budget_deploy.resolve_consent("BUDGET-PAPER", reviewed, resolved_parameters={"period": 6})
    changed = reviewed.model_copy(update={"budget": reviewed.budget.model_copy(update={"amount_usd": "600.00"})})
    with pytest.raises(BudgetUnavailable):
        budget_deploy.resolve_consent("BUDGET-PAPER", changed, resolved_parameters={"period": 5})
    authority[2].policy = replace(authority[2].policy, revision=2)
    append_risk_policy(authority[0], policy=authority[2].policy, expected_revision=1)
    assert budget_deploy.preview_budget("BUDGET-PAPER", reviewed, resolved_parameters={}).state == "unavailable"


def test_dry_run_never_copies_parent_cash_or_risk(authority) -> None:
    authority[2].observation = None
    request = _request(execution_mode="dry_run", budget=DeploymentBudgetInput(amount_usd="2000", risk_revision=0))
    preview = budget_deploy.preview_budget("BUDGET-PAPER", request, resolved_parameters={})
    assert preview.state == "ready" and preview.world == "synthetic"
    assert preview.custody_account_id == "sim:review-a" and preview.unreserved_usd is None
    assert [item.key for item in preview.shortcuts] == ["position_headroom"]


async def test_recovery_returns_committed_outcome_even_after_evidence_expires(authority) -> None:
    repo, _, snapshot = authority
    request = _request(budget=DeploymentBudgetInput(amount_usd="500", risk_revision=1))
    terms = request.exit_terms.seal()
    repo.register_strategy_instance(strategy_instance_id=request.strategy_instance_id, symbol="SPY", config_hash="seal", exit_terms=terms)
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    gate.publish(snapshot.observation)
    submit_budgeted_deploy(repo, strategy_instance_id=request.strategy_instance_id, lifecycle_run_id="run", world="real_paper", committed_cents=50_000, configuration_hash="seal", exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")), risk_revision=1, actor="owner", envelope=gate, minimum_position_cost=Decimal(100), request_fingerprint=budget_deploy.request_fingerprint(request, custody_account_id=repo.account_id, world="real_paper"))
    snapshot.observation = None
    receipt = await budget_deploy.command_receipt(repo.account_id, request.strategy_instance_id, request)
    assert receipt.status == "pending" and receipt.committed_usd == "500.00"
    altered = request.model_copy(update={"symbol": "QQQ"})
    with pytest.raises(BudgetUnavailable):
        await budget_deploy.command_receipt(repo.account_id, request.strategy_instance_id, altered)


def test_preview_asks_ibkr_for_an_unwatched_symbol_and_recovers_once_its_quote_lands(
    authority: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2550 review: no bot watches a first-time symbol, so the review itself must
    register IBKR demand; otherwise the quote never arrives and Deploy deadlocks."""
    store = market_liveness.get_market_liveness_store()
    _publish_book()
    monkeypatch.setattr("app.utils.timestamps.now_ms_utc", lambda: NOON)

    waiting = budget_deploy.preview_budget("BUDGET-PAPER", _request(symbol="NVDA"), resolved_parameters={})

    assert store.requested_symbols() == ("NVDA",)
    assert waiting.state == "awaiting_price" and "fresh IBKR price" in waiting.detail
    assert waiting.review_token is None and waiting.shortcuts == ()
    _publish_book(("NVDA", 180))
    ready = budget_deploy.preview_budget("BUDGET-PAPER", _request(symbol="NVDA"), resolved_parameters={})
    assert ready.state == "ready" and ready.estimated_price_usd == "180.00"
    assert "position_headroom" in {shortcut.key for shortcut in ready.shortcuts}


def test_fractional_cent_consent_is_rejected_at_wire_boundary() -> None:
    with pytest.raises(ValidationError):
        DeploymentBudgetInput(amount_usd="100.001", risk_revision=0)


def _committed_view(authority: tuple) -> tuple:
    repo, runtime, snapshot = authority
    terms = _request().exit_terms.seal()
    config = {"symbol": "SPY", "quantity": 1}
    from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
    repo.register_strategy_instance(strategy_instance_id="view", symbol="SPY", config_hash=canonical_sha256(config),
        config_json=canonicalize(config), exit_terms=terms)
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    gate.publish(snapshot.observation)
    runtime.envelope_sync.envelope = gate
    submit_budgeted_deploy(repo, strategy_instance_id="view", lifecycle_run_id="view-run", world="real_paper",
        committed_cents=20_000, configuration_hash=canonical_sha256(config), exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")),
        risk_revision=1, actor="owner", envelope=gate, minimum_position_cost=Decimal("100.02"))
    return repo, runtime, gate


def test_budget_read_uses_the_sealed_next_position_and_current_cash(authority: tuple) -> None:
    repo, runtime, gate = _committed_view(authority)
    assert budget_deploy._budget_view(runtime, "view").entry_eligible
    before = repo.custody_transitions()
    _publish_book(("SPY", 200))
    view = budget_deploy._budget_view(runtime, "view")
    assert not view.entry_eligible and "200.01 USD" in view.detail
    assert view.free_usd == "200.00"
    _publish_book(("SPY", 100))
    gate.publish(replace(gate.latest_observation(), cash_available_usd=100))
    assert not budget_deploy._budget_view(runtime, "view").entry_eligible
    assert "account cash" in budget_deploy._budget_view(runtime, "view").detail
    assert repo.custody_transitions() == before


def test_budget_read_rejudges_risk_without_creating_a_hold(authority: tuple) -> None:
    repo, runtime, gate = _committed_view(authority)
    gate.publish(replace(gate.latest_observation(), equity_usd=899))
    before = repo.custody_transitions()
    view = budget_deploy._budget_view(runtime, "view")
    assert not view.entry_eligible and "loss limit is breached" in view.detail
    assert repo.custody_transitions() == before
    assert gate.latest_observation() is not None


def test_budget_read_requires_a_fresh_price_and_observes_existing_holds(authority: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold
    from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE
    repo, runtime, _ = _committed_view(authority)
    _publish_book()
    view = budget_deploy._budget_view(runtime, "view")
    assert not view.entry_eligible and "fresh IBKR price" in view.detail
    # The read asks IBKR for the sealed symbol, so a bot no running process
    # watches still gets a price to judge its next position against.
    monkeypatch.setattr("app.utils.timestamps.now_ms_utc", lambda: NOON)
    assert "SPY" in market_liveness.get_market_liveness_store().requested_symbols()
    assert view.committed_usd == "200.00"
    raise_account_hold(repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, evidence_refs=["retained-loss"], cause_facts={
        "day_start_ms": NOON-1, "day_pnl_usd": -100, "loss_limit_usd": 100, "last_equity_usd": 1000, "observed_at_ms": NOON,
    })
    before = repo.custody_transitions()
    view = budget_deploy._budget_view(runtime, "view")
    assert not view.entry_eligible and "loss hold stands" in view.detail
    assert repo.custody_transitions() == before


def test_budget_preview_refuses_post_observation_fill_without_mutating_risk(authority: tuple) -> None:
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.contract.models import BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

    repo, _, gate = _committed_view(authority)
    assert budget_deploy.preview_budget(repo.account_id, _request(), resolved_parameters={}).state == "ready"
    accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="view", decision_id="later-fill",
                           lifecycle_run_id="view-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
                           reference_price=100, envelope=gate)
    _append_slice(repo, accepted, execution_id="after-observation", quantity=1, price=100, source_event_at_ms=NOON, fee=0)
    before = repo.custody_transitions()
    observation = gate.latest_observation()
    preview = budget_deploy.preview_budget(repo.account_id, _request(), resolved_parameters={})
    assert preview.state == "unavailable" and "Executions changed" in preview.detail
    assert preview.review_token is None
    assert repo.custody_transitions() == before
    assert gate.latest_observation() == observation


async def test_dry_run_receipt_survives_a_crash_before_the_launch_recorded_its_binding(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#2550 review: Deploy commits the private authority's budget before the runner
    records the binding. A crash in between must leave the committed command
    recoverable from that authority's own durable evidence, not a 404 forever.
    Its recovery only releases: the orphaned run stops and the command fails."""
    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
    from app.schemas.deployment_budget import DeployBudgetConsent
    from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
    from app.services.bot_runner import BotTaskRegistry
    from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS

    sid = "crashed-before-binding"
    clock = _TestClock(NOON)
    binding = BrokerBotBinding(
        strategy_instance_id=sid, strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY", mode="dry_run",
        quantity=1, action_plan=alpaca_v1_action_plan("SPY"), run_id="run-1", created_at_ms=0,
        sealed_account_id=f"sim:{sid}", exit_terms=TERMS, budget_consent=DeployBudgetConsent(
            committed_cents=50_000, risk_revision=0, actor="owner", request_fingerprint="reviewed", world="synthetic"),
    )
    primary_repo = ClerkSqliteRepository.initialize(account_id="PARENT", artifacts_root=tmp_path / "primary", clock=clock)

    def registry() -> BotTaskRegistry:
        return BotTaskRegistry(tmp_path, feed_resolver=lambda: None, now_ms=clock, boot_recovery_required=False)

    try:
        # The Deploy that crashed: its private authority committed the budget
        # and run, then the process died before record_launch wrote a binding.
        deploying = registry()
        authority = deploying._authority_for(binding)
        await authority.ensure_recoverable()
        runtime = get_clerk_runtime(f"sim:{sid}")
        runtime.clerk._quote_source = lambda symbol, now: SimpleNamespace(ask=100)
        await runtime.clerk.register_strategy_run(binding)
        await authority.release_if_unused()
        assert get_clerk_runtime(f"sim:{sid}") is None

        recovered = registry()
        assert recovered.bindings_for_broker("alpaca") == []
        monkeypatch.setattr(budget_deploy, "_primary", lambda account: SimpleNamespace(sqlite_repository=primary_repo))
        monkeypatch.setattr(budget_deploy, "get_bot_task_registry", lambda: recovered)

        receipt = await budget_deploy.command_receipt("PARENT", sid)

        assert receipt is not None
        assert receipt.status == "failed" and receipt.world == "synthetic" and receipt.committed_usd == "500.00"
        assert receipt.run_id == f"{sid}:run-1"
        # A read composes nothing that outlives it and records no binding.
        assert get_clerk_runtime(f"sim:{sid}") is None
        assert recovered.bindings_for_broker("alpaca") == []
        # An identity no private authority ever held still reads the primary.
        assert await budget_deploy.command_receipt("PARENT", "never-deployed") is None
    finally:
        await close_synthetic_clerk_runtimes()
        primary_repo.close()


# ── Account money: the one read every money bar draws (PRD #2560) ───────────


def _segments(view) -> list[tuple[str, str, str, int]]:
    return [(segment.kind, segment.label, segment.amount_usd, segment.share_bps) for segment in view.segments]


def test_account_money_is_the_running_bot_beside_free_to_deploy(authority: tuple) -> None:
    repo, _, _ = _committed_view(authority)

    view = budget_deploy.account_money_view(repo.account_id)

    assert view.state == "ready" and view.world == "real_paper" and view.account_id == repo.account_id
    assert _segments(view) == [("bot", "view", "200.00", 2_000), ("free", "free to deploy", "800.00", 8_000)]
    assert (view.total_usd, view.cash_usd, view.in_bots_usd, view.free_to_deploy_usd) == ("1000.00", "1000.00", "200.00", "800.00")
    assert (view.held_by_stopped_usd, view.outside_bots_usd, view.account_charges_usd) == ("0.00", "0.00", "0.00")
    assert (view.equity_usd, view.today_pnl_usd, view.open_pnl_usd) == ("1000.00", "0.00", "0.00")
    parts = view.segments[0].parts
    assert parts is not None and (parts.in_shares_usd, parts.pending_usd, parts.free_usd, parts.free_bps) == ("0.00", "0.00", "200.00", 10_000)
    assert view.segments[0].shortfall_usd == "0.00" and view.observed_at_ms == NOON
    assert view.detail == "Cash plus shares at the price paid."


def test_free_to_deploy_is_the_deploy_previews_unreserved_cash_and_money_after_carves_new(authority: tuple) -> None:
    repo, _, _ = _committed_view(authority)
    view = budget_deploy.account_money_view(repo.account_id)

    preview = budget_deploy.preview_budget(repo.account_id, _request(), resolved_parameters={})
    assert view.free_to_deploy_usd == preview.unreserved_usd == "800.00"
    assert preview.money_after == view  # no amount yet: today's bar, read from the same observation

    request = _request(budget=DeploymentBudgetInput(amount_usd="300", risk_revision=1))
    after = budget_deploy.preview_budget(repo.account_id, request, resolved_parameters={}).money_after
    assert after is not None and after.state == "ready"
    assert _segments(after) == [
        ("bot", "view", "200.00", 2_000), ("new", "new bot", "300.00", 3_000), ("free", "free to deploy", "500.00", 5_000),
    ]
    assert after.total_usd == view.total_usd


def test_money_after_is_absent_when_the_preview_refuses_and_for_dry_run(authority: tuple) -> None:
    repo, _, _ = _committed_view(authority)
    too_much = _request(budget=DeploymentBudgetInput(amount_usd="900", risk_revision=1))
    refused = budget_deploy.preview_budget(repo.account_id, too_much, resolved_parameters={})
    assert refused.state == "unavailable" and "Only $800.00 is unreserved" in refused.detail
    assert refused.money_after is None
    dry = _request(execution_mode="dry_run", budget=DeploymentBudgetInput(amount_usd="2000", risk_revision=0))
    assert budget_deploy.preview_budget(repo.account_id, dry, resolved_parameters={}).money_after is None


@pytest.mark.parametrize("observation", ["withdrawn", "stale"])
def test_a_missing_or_stale_cash_observation_is_unavailable_never_zero(authority: tuple, observation: str) -> None:
    repo, runtime, _ = _committed_view(authority)
    gate = runtime.envelope_sync.envelope
    if observation == "withdrawn":
        gate.withdraw()
    else:
        repo.clock.advance(60_000)

    view = budget_deploy.account_money_view(repo.account_id)

    from app.broker.alpaca.clerk.sqlite.risk_admission import current_risk_readiness
    canonical = current_risk_readiness(repo, envelope=gate, now_ms=repo.clock())
    assert view.state == "unavailable" and view.detail == canonical.detail
    assert view.segments == () and view.total_usd is None and view.free_to_deploy_usd is None


def _unlimited_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch, *, cutover: bool = True) -> ClerkSqliteRepository:
    """A Paper account whose owner never applied risk limits (hurdle H7)."""
    repo = ClerkSqliteRepository.initialize(account_id="NO-LIMITS", artifacts_root=tmp_path, clock=_TestClock(NOON))
    if cutover:
        commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    # With no limits the sync cannot judge the account, so it publishes no observation.
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    snapshot = SimpleNamespace(observation=None, policy=None, hold=None)
    runtime = SimpleNamespace(selected_account_id=repo.account_id, account_authority_kind="real_paper", sqlite_repository=repo,
                              envelope_sync=SimpleNamespace(envelope=gate, risk_snapshot=lambda: snapshot))
    monkeypatch.setattr(budget_deploy, "get_active_clerk_runtime", lambda: runtime)
    return repo


def test_an_account_without_risk_limits_is_unavailable_for_the_canonical_readiness_reason(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hurdle H7: the money read names the same cause the Deploy preview does.

    The wording belongs to the one readiness check (``risk_admission``);
    this read never authors a second copy of it, and never shows $0.
    """
    from app.broker.alpaca.clerk.sqlite.risk_admission import current_risk_readiness

    repo = _unlimited_runtime(tmp_path, monkeypatch)
    market_liveness.reset_market_liveness_store_for_testing()
    _publish_book(("SPY", 100))
    try:
        view = budget_deploy.account_money_view(repo.account_id)
        preview = budget_deploy.preview_budget(repo.account_id, _request(), resolved_parameters={})
        canonical = current_risk_readiness(repo, envelope=LiveEnvelopeGate(values=None, custody_is_simulated=False), now_ms=NOON)
    finally:
        market_liveness.reset_market_liveness_store_for_testing()
        repo.close()
    assert not canonical.allowed
    assert view.state == "unavailable" and view.detail == canonical.detail == preview.detail
    assert view.total_usd is None and view.segments == ()


def test_an_account_not_switched_to_budgets_reads_legacy_with_its_settings_action(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _unlimited_runtime(tmp_path, monkeypatch, cutover=False)
    try:
        view = budget_deploy.account_money_view(repo.account_id)
    finally:
        repo.close()
    assert view.state == "legacy" and "Switch it in Settings" in view.detail and view.segments == ()


def test_a_dry_run_budget_never_appears_on_the_accounts_money(authority: tuple, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _, _ = _committed_view(authority)
    before = budget_deploy.account_money_view(repo.account_id)
    dry_repo = ClerkSqliteRepository.initialize(account_id="sim:dry-bot", artifacts_root=tmp_path / "dry", clock=_TestClock(NOON))
    try:
        from app.broker.alpaca.clerk.et_day import et_day_window_ms

        commit_budget_authority_cutover(dry_repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
        terms = _request().exit_terms.seal()
        dry_repo.register_strategy_instance(strategy_instance_id="dry-bot", symbol="SPY", config_hash="seal", exit_terms=terms)
        session = et_day_window_ms(NOON)[0]
        dry_gate = LiveEnvelopeGate(values=None, custody_is_simulated=True)
        dry_gate.publish(AccountObservation(observed_at_ms=NOON, broker_cash_usd=2000, cash_available_usd=2000,
            last_equity_usd=2000, position_count=0, equity_usd=2000, simulation_session_start_ms=session,
            risk_equity_window_start_ms=session, risk_cash_flow_evidence_complete=True, risk_cash_flow_window_start_ms=session))
        # The Dry Run's private authority holds its own simulated budget.
        submit_budgeted_deploy(dry_repo, strategy_instance_id="dry-bot", lifecycle_run_id="dry-run", world="synthetic",
            committed_cents=200_000, configuration_hash="seal", exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")),
            risk_revision=0, actor="owner", envelope=dry_gate, minimum_position_cost=Decimal("100.02"))
        assert dry_repo.deployment_budget("dry-bot") is not None
        monkeypatch.setattr(budget_deploy, "get_clerk_runtime", lambda account_id: SimpleNamespace(sqlite_repository=dry_repo))

        after = budget_deploy.account_money_view(repo.account_id)
    finally:
        dry_repo.close()
    assert after == before
    assert all(segment.strategy_instance_id != "dry-bot" for segment in after.segments)


def test_shadow_draws_its_bar_from_its_own_shadow_pool(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="shadow:LIVE-1", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
        gate = LiveEnvelopeGate(values=None, custody_is_simulated=True)
        gate.publish(AccountObservation(observed_at_ms=NOON, broker_cash_usd=5000, cash_available_usd=2500,
            last_equity_usd=2500, position_count=0, equity_usd=2500))
        runtime = SimpleNamespace(selected_account_id=repo.account_id, account_authority_kind="shadow", sqlite_repository=repo,
                                  envelope_sync=SimpleNamespace(envelope=gate))
        monkeypatch.setattr(budget_deploy, "get_active_clerk_runtime", lambda: runtime)

        view = budget_deploy.account_money_view("LIVE-1")
    finally:
        repo.close()
    # The simulated pool's own cash, never the real account's $5,000.
    assert view.state == "ready" and view.world == "shadow" and view.account_id == "LIVE-1"
    assert _segments(view) == [("free", "free to deploy", "2500.00", 10_000)]


def test_bot_budget_read_carries_the_same_shaded_parts_as_its_segment(authority: tuple) -> None:
    repo, runtime, _ = _committed_view(authority)
    segment = budget_deploy.account_money_view(repo.account_id).segments[0]
    assert budget_deploy._budget_view(runtime, "view").parts == segment.parts


async def test_account_money_endpoint_serves_the_view_and_refuses_an_unserved_account(authority: tuple) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.routers import broker_v2_panel

    repo, _, _ = _committed_view(authority)
    app = FastAPI()
    app.include_router(broker_v2_panel.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        served = await client.get(f"/api/brokers/alpaca/accounts/{repo.account_id}/money")
        other = await client.get("/api/brokers/alpaca/accounts/SOMEONE-ELSE/money")

    assert served.status_code == 200
    assert served.json()["free_to_deploy_usd"] == "800.00" and served.json()["segments"][0]["kind"] == "bot"
    assert other.status_code == 503 and "custody authority is unavailable" in other.json()["detail"]["why"]


def test_money_that_does_not_add_up_is_withheld_loudly_and_deploy_still_previews(
    authority: tuple, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from app.broker.alpaca.clerk.account_money import MoneyConservationError

    repo, _, _ = _committed_view(authority)

    def unconserved(*args, **kwargs):
        raise MoneyConservationError("parts 1 != total 2")

    monkeypatch.setattr(budget_deploy, "money_bar", unconserved)

    view = budget_deploy.account_money_view(repo.account_id)
    preview = budget_deploy.preview_budget(repo.account_id, _request(), resolved_parameters={})

    assert view.state == "unavailable" and "does not add up" in view.detail and view.segments == ()
    assert any(getattr(record, "action", None) == "account_money_unconserved" for record in caplog.records)
    assert preview.state == "ready" and preview.money_after is not None and preview.money_after.state == "unavailable"


# ── This bot's money: the bot page's statement (PRD #2560 slice 3) ──────────


def _statement(view: DeploymentBudgetView) -> list[tuple[str, str, bool]]:
    return [(line.label, line.amount_usd, line.total) for line in view.statement]


def test_a_running_bot_reads_its_money_as_a_statement(authority: tuple) -> None:
    _, runtime, _ = _committed_view(authority)

    view = budget_deploy._budget_view(runtime, "view")

    assert (view.headline, view.entry_eligible) == ("Ready for its next entry", True)
    assert _statement(view) == [
        ("Budget set aside at deploy", "200.00", False),
        ("Realized gains and losses", "0.00", False),
        ("Fees", "0.00", False),
        ("Balance", "200.00", True),
        ("In shares, at cost", "0.00", False),
        ("Waiting in entry orders", "0.00", False),
        ("Free to trade", "200.00", False),
        # One SPY at $100.001 plus its modelled BUY fee costs $100.02.
        ("Short of its next entry", "0.00", False),
    ]
    assert view.note == "The budget limits new entries. Market fills and losses can go past it."


def test_a_bot_short_of_its_next_entry_says_by_how_much(authority: tuple) -> None:
    _, runtime, _ = _committed_view(authority)
    _publish_book(("SPY", 200))

    view = budget_deploy._budget_view(runtime, "view")

    assert view.headline == "Its next entry waits" and "200.01 USD" in view.detail
    assert _statement(view)[-1] == ("Short of its next entry", "0.01", False)


def test_holding_a_position_reads_as_holding_never_as_a_fault(authority: tuple) -> None:
    """Hurdle H25: a bot simply holding its position read "Budget or account
    risk unavailable" and "a fresh ENTER waits for a proved EXIT to flat"."""
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.contract.models import BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

    repo, runtime, gate = _committed_view(authority)
    accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="view", decision_id="holding",
                           lifecycle_run_id="view-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
                           reference_price=100, envelope=gate)
    _append_slice(repo, accepted, execution_id="holding-fill", quantity=1, price=100, source_event_at_ms=NOON, fee=0)

    view = budget_deploy._budget_view(runtime, "view")

    assert view.state == "ready" and not view.entry_eligible
    assert (view.headline, view.detail) in {
        ("Holding its position", "Its next entry waits until this position is sold."),
        ("Entering its position", "Its entry is still working. The next entry waits until this one finishes."),
    }
    words = f"{view.headline} {view.detail}".lower()
    assert all(fault not in words for fault in ("unavailable", "enter ", "exit ", "clerk"))
    assert ("In shares, at cost", "100.00", False) in _statement(view)
    assert ("Free to trade", "100.00", False) in _statement(view)


def test_a_dry_run_bot_shows_its_own_money_after_a_fill_without_account_evidence(tmp_path) -> None:
    """Hurdle H27: once a Dry Run's simulated buy filled, every figure read
    "Unknown" under "Fresh account evidence ... unavailable". Its money is its
    committed starting cash less what its fills spent: no mark and no account
    observation is needed to show it."""
    from app.broker.alpaca.clerk.sqlite.simulated_account import SimulatedAccountProjection
    from tests.broker.alpaca.clerk.sqlite.conftest import DAY_PNL_SID
    from tests.broker.alpaca.clerk.sqlite.test_simulated_account import _deploy, _enter, _fill

    repo = ClerkSqliteRepository.initialize(account_id=f"sim:{DAY_PNL_SID}", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        projection = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path, initial_cash=Decimal(1000))
        gate = _deploy(repo, projection, cents=100_000)
        _fill(repo, _enter(repo, gate), key="simulated-buy", side="BUY", quantity=2, price=100)
        # No price has arrived since the fill and no observation is published:
        # the feed is down, or the bot is stopped.
        runtime = SimpleNamespace(account_authority_kind="synthetic", sqlite_repository=repo,
                                  envelope_sync=SimpleNamespace(envelope=LiveEnvelopeGate(values=None, custody_is_simulated=True)))

        view = budget_deploy._budget_view(runtime, DAY_PNL_SID)
    finally:
        repo.close()

    assert view.state == "ready" and view.world == "synthetic"
    assert "account evidence" not in view.detail.lower()
    # The simulated BUY of 2 carries its modelled CAT fee, a cent at most.
    assert _statement(view) == [
        ("Budget set aside at deploy", "1000.00", False),
        ("Realized gains and losses", "0.00", False),
        ("Fees", "-0.01", False),
        ("Balance", "999.99", True),
        ("In shares, at cost", "200.00", False),
        ("Waiting in entry orders", "0.00", False),
        ("Free to trade", "799.99", False),
    ]


def test_a_stopped_bot_statement_shows_what_was_released_and_what_is_still_held(authority: tuple) -> None:
    from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.contract.models import BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

    repo, runtime, gate = _committed_view(authority)
    accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="view", decision_id="held",
                           lifecycle_run_id="view-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
                           reference_price=100, envelope=gate)
    _append_slice(repo, accepted, execution_id="held-fill", quantity=1, price=100, source_event_at_ms=NOON, fee=0)
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="view", lifecycle_run_id="view-run", clock=repo.clock)
    # Cash observed after the fill, so its cost is in cash and nothing waits.
    repo.clock.advance(10_000)
    gate.publish(replace(gate.latest_observation(), observed_at_ms=repo.clock(), cash_available_usd=900))

    view = budget_deploy._budget_view(runtime, "view")

    assert view.state == "ready" and not view.entry_eligible
    assert view.headline == "Stopped · still holds shares"
    assert view.detail == "Its free budget was released when it stopped. The money in its shares comes back when they are sold."
    assert _statement(view) == [
        ("Budget set aside at deploy", "200.00", False),
        ("Realized gains and losses", "0.00", False),
        ("Fees", "0.00", False),
        ("Balance", "200.00", True),
        ("Released at stop", "100.00", False),
        ("Still in shares, at cost", "100.00", False),
        ("Waiting on orders, fills or fees", "0.00", False),
    ]
    assert view.note is None
