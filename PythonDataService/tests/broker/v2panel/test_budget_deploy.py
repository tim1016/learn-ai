"""Trader money review uses custody facts and immutable consent, not UI math."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate, observation_is_fresh
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
    (DeploymentBudgetView, "observed_at_ms", dict(state="ready", detail="Ready", world="real_paper", strategy_instance_id="bot")),
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


class _Sync:
    """The sync's read contract as the money views use it.

    ``reading`` is the last account reading; the envelope publishes (admits)
    it only when the account is judged safe. Withdrawing admission never
    forgets the reading, exactly as ``LiveEnvelopeSync`` keeps
    ``_last_reading`` (see test_live_envelope_sync) -- a fake that tied the
    two together would hide review A6.
    """

    def __init__(self, gate: LiveEnvelopeGate, reading: AccountObservation | None, snapshot: SimpleNamespace | None = None) -> None:
        self.envelope = gate
        self.reading = reading
        self.snapshot = snapshot

    def risk_snapshot(self) -> SimpleNamespace | None:
        return self.snapshot

    def display_observation(self, now_ms: int) -> AccountObservation | None:
        reading = self.reading
        if reading is None or not observation_is_fresh(reading, now_ms=now_ms, max_age_ms=self.envelope.observation_max_age_ms):
            return None
        return reading


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
    runtime = SimpleNamespace(selected_account_id=repo.account_id, account_authority_kind="real_paper", sqlite_repository=repo, envelope_sync=_Sync(gate, observation, snapshot))
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


def _hold_one_share(repo: ClerkSqliteRepository, gate: LiveEnvelopeGate) -> None:
    """Bot ``view`` buys 1 SPY at $100; the reading's cash has not seen it yet."""
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.contract.models import BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

    accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="view", decision_id="held",
                            lifecycle_run_id="view-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
                            reference_price=100, envelope=gate)
    _append_slice(repo, accepted, execution_id="held", quantity=1, price=100, source_event_at_ms=NOON, fee=0)


async def test_account_money_is_the_running_bot_beside_free_to_deploy(authority: tuple) -> None:
    repo, _, _ = _committed_view(authority)

    view = await budget_deploy.account_money_view(repo.account_id)

    assert view.state == "ready" and view.world == "real_paper" and view.account_id == repo.account_id
    assert _segments(view) == [("bot", "view", "200.00", 2_000), ("free", "free to deploy", "800.00", 8_000)]
    assert (view.total_usd, view.cash_usd, view.in_bots_usd, view.free_to_deploy_usd) == ("1000.00", "1000.00", "200.00", "800.00")
    assert (view.held_by_stopped_usd, view.outside_bots_usd, view.account_charges_usd) == ("0.00", "0.00", "0.00")
    assert (view.settling_usd, view.account_shortfall_usd, view.stopped_holding_count) == ("0.00", "0.00", 0)
    assert (view.equity_usd, view.today_pnl_usd, view.open_pnl_usd, view.open_pnl_detail) == ("1000.00", "0.00", "0.00", None)
    parts = view.segments[0].parts
    assert parts is not None and (parts.in_shares_usd, parts.pending_usd, parts.free_usd, parts.free_bps) == ("0.00", "0.00", "200.00", 10_000)
    assert view.segments[0].shortfall_usd == "0.00" and view.observed_at_ms == NOON
    assert (view.segments[0].palette_index, view.segments[1].palette_index) == (0, None)
    assert view.detail == "Cash plus shares at the price paid."


async def test_free_to_deploy_is_the_deploy_previews_unreserved_cash_and_money_after_carves_new(authority: tuple) -> None:
    repo, _, _ = _committed_view(authority)
    view = await budget_deploy.account_money_view(repo.account_id)

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


async def test_the_deploy_preview_reads_the_account_once_under_its_fence(authority: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review A5: free to deploy = unreserved by construction, from one projection."""
    repo, _, _ = _committed_view(authority)
    calls: list[str] = []
    for name in ("account_budget", "account_money"):
        real = getattr(repo, name)
        monkeypatch.setattr(repo, name, lambda *a, _real=real, _name=name, **k: (calls.append(_name), _real(*a, **k))[1])

    preview = budget_deploy.preview_budget(repo.account_id, _request(), resolved_parameters={})

    assert preview.state == "ready" and preview.money_after is not None
    assert calls == ["account_money"]


async def test_a_refused_amount_still_returns_the_bar_the_money_step_draws(authority: tuple) -> None:
    repo, _, _ = _committed_view(authority)
    today = await budget_deploy.account_money_view(repo.account_id)
    for amount, why in (("900", "Only $800.00 is unreserved"), ("50", "at least $100.02")):
        refused = budget_deploy.preview_budget(
            repo.account_id, _request(budget=DeploymentBudgetInput(amount_usd=amount, risk_revision=1)), resolved_parameters={},
        )
        assert refused.state == "unavailable" and why in refused.detail
        assert refused.review_token is None and refused.unreserved_usd == "800.00"
        assert refused.money_after == today  # the bar, with no new slice
    dry = _request(execution_mode="dry_run", budget=DeploymentBudgetInput(amount_usd="2000", risk_revision=0))
    assert budget_deploy.preview_budget(repo.account_id, dry, resolved_parameters={}).money_after is None


async def test_a_loss_hold_withdraws_admission_but_never_the_accounts_money(authority: tuple) -> None:
    """Review A6: the sync withdraws its admission observation whenever the
    account is not judged safe (a loss hold, an unjudgeable day). The money
    was still read, so the bar and Alpaca's figures stay."""
    repo, runtime, gate = _committed_view(authority)
    gate.withdraw()

    view = await budget_deploy.account_money_view(repo.account_id)

    assert view.state == "ready" and view.free_to_deploy_usd == "800.00"
    assert (view.equity_usd, view.today_pnl_usd) == ("1000.00", "0.00")
    assert not budget_deploy._budget_view(runtime, "view").entry_eligible


async def test_a_stale_reading_is_unavailable_never_zero(authority: tuple) -> None:
    repo, _, _ = _committed_view(authority)
    repo.clock.advance(60_000)

    view = await budget_deploy.account_money_view(repo.account_id)

    assert view.state == "unavailable" and view.detail == budget_deploy._NO_FRESH_READING
    assert "Settings" in view.detail and "Configuration" not in view.detail
    assert view.segments == () and view.total_usd is None and view.free_to_deploy_usd is None
    assert view.equity_usd is None and view.today_pnl_usd is None


async def test_a_bar_that_cannot_be_drawn_keeps_alpacas_equity_and_today(authority: tuple) -> None:
    """Review A6: an unknown claim blanks the bar, never the broker figures."""
    from app.broker.alpaca.clerk.sqlite.manual_orders import accept_manual_order
    from app.broker.contract.models import BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.test_manual_orders import LEG_ID, OPERATOR_ID, TICKET_ID

    repo, _, _ = _committed_view(authority)
    accept_manual_order(repo, account_id=repo.account_id, operator_id=OPERATOR_ID, ticket_id=TICKET_ID,
                        leg_id=LEG_ID, leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1))

    view = await budget_deploy.account_money_view(repo.account_id)

    assert view.state == "unavailable" and "manual order is still working" in view.detail
    assert "before assigning or spending a budget" not in view.detail
    assert (view.equity_usd, view.today_pnl_usd, view.observed_at_ms) == ("1000.00", "0.00", NOON)
    assert view.segments == () and view.total_usd is None


async def test_an_overdrawn_account_draws_its_bar_and_reports_the_shortfall(authority: tuple) -> None:
    """Review A6: overdrawn is a known state -- drawn, with the overrun as data."""
    repo, runtime, _ = _committed_view(authority)
    runtime.envelope_sync.reading = replace(runtime.envelope_sync.reading, cash_available_usd=150)

    view = await budget_deploy.account_money_view(repo.account_id)

    assert view.state == "ready" and view.account_shortfall_usd == "50.00"
    assert _segments(view) == [("bot", "view", "200.00", 10_000), ("free", "free to deploy", "0.00", 0)]
    assert (view.total_usd, view.free_to_deploy_usd) == ("150.00", "0.00")


async def test_open_pnl_is_canonical_fifo_or_withheld_with_its_reason(authority: tuple) -> None:
    """Review A4: never Alpaca's equity less the bar's cost total."""
    repo, runtime, gate = _committed_view(authority)
    _hold_one_share(repo, gate)
    # Alpaca marks the share $5 up: the retired formula would have said $5.00.
    runtime.envelope_sync.reading = replace(runtime.envelope_sync.reading, equity_usd=1005)

    real = await budget_deploy.account_money_view(repo.account_id)
    assert real.state == "ready" and real.total_usd == "1000.00" and real.equity_usd == "1005.00"
    assert real.open_pnl_usd is None and real.open_pnl_detail == budget_deploy._OPEN_PNL_UNPRICED

    # A simulated reading values its own lots with its own marks (fifo.open_pnl).
    runtime.envelope_sync.reading = replace(runtime.envelope_sync.reading, simulation_session_start_ms=NOON - 1, unrealized_pl_usd=4.2)
    simulated = await budget_deploy.account_money_view(repo.account_id)
    assert (simulated.open_pnl_usd, simulated.open_pnl_detail) == ("4.20", None)


async def test_an_account_without_risk_limits_still_shows_its_money_while_deploy_names_the_limits(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hurdle H7 x review A6: with no limits the sync cannot judge the account,
    so it admits nothing -- but it read the cash. The money read shows it; the
    Deploy preview names the missing limits (``risk_admission``'s wording)."""
    repo = ClerkSqliteRepository.initialize(account_id="NO-LIMITS", artifacts_root=tmp_path, clock=_TestClock(NOON))
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    reading = AccountObservation(observed_at_ms=NOON, broker_cash_usd=1000, cash_available_usd=1000,
                                 last_equity_usd=1000, position_count=0, equity_usd=1000)
    sync = _Sync(LiveEnvelopeGate(values=None, custody_is_simulated=False), reading, SimpleNamespace(observation=None, policy=None, hold=None))
    runtime = SimpleNamespace(selected_account_id=repo.account_id, account_authority_kind="real_paper", sqlite_repository=repo, envelope_sync=sync)
    monkeypatch.setattr(budget_deploy, "get_active_clerk_runtime", lambda: runtime)
    market_liveness.reset_market_liveness_store_for_testing()
    _publish_book(("SPY", 100))
    try:
        view = await budget_deploy.account_money_view(repo.account_id)
        preview = budget_deploy.preview_budget(repo.account_id, _request(), resolved_parameters={})
    finally:
        market_liveness.reset_market_liveness_store_for_testing()
        repo.close()
    assert view.state == "ready" and _segments(view) == [("free", "free to deploy", "1000.00", 10_000)]
    assert preview.state == "unavailable" and preview.money_after is None


async def test_an_account_not_switched_to_budgets_reads_legacy_with_its_settings_action(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="LEGACY", artifacts_root=tmp_path, clock=_TestClock(NOON))
    runtime = SimpleNamespace(selected_account_id=repo.account_id, account_authority_kind="real_paper", sqlite_repository=repo,
                              envelope_sync=_Sync(LiveEnvelopeGate(values=None, custody_is_simulated=False), None))
    monkeypatch.setattr(budget_deploy, "get_active_clerk_runtime", lambda: runtime)
    try:
        view = await budget_deploy.account_money_view(repo.account_id)
    finally:
        repo.close()
    assert view.state == "legacy" and "Switch it in Settings" in view.detail and view.segments == ()


async def test_a_dry_run_budget_never_appears_on_the_accounts_money(authority: tuple, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review: a real Dry Run authority, installed and holding its budget, with
    the real runtime lookup and bot registry in place -- nothing is patched
    away that the money read could consult."""
    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
    from app.schemas.deployment_budget import DeployBudgetConsent
    from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
    from app.services.bot_runner import BotTaskRegistry
    from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS

    repo, _, _ = _committed_view(authority)
    before = await budget_deploy.account_money_view(repo.account_id)
    sid = "dry-bot"
    binding = BrokerBotBinding(
        strategy_instance_id=sid, strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY", mode="dry_run",
        quantity=1, action_plan=alpaca_v1_action_plan("SPY"), run_id="run-1", created_at_ms=0,
        sealed_account_id=f"sim:{sid}", exit_terms=TERMS, budget_consent=DeployBudgetConsent(
            committed_cents=50_000, risk_revision=0, actor="owner", request_fingerprint="reviewed", world="synthetic"),
    )
    registry = BotTaskRegistry(tmp_path / "dry", feed_resolver=lambda: None, now_ms=repo.clock, boot_recovery_required=False)
    try:
        dry_authority = registry._authority_for(binding)
        await dry_authority.ensure_recoverable()
        dry = get_clerk_runtime(f"sim:{sid}")
        dry.clerk._quote_source = lambda symbol, now: SimpleNamespace(ask=100)
        await dry.clerk.register_strategy_run(binding)
        # Positive control: the Dry Run's own authority holds a real budget.
        assert dry.sqlite_repository.deployment_budget(sid)["committed_cents"] == 50_000
        monkeypatch.setattr(budget_deploy, "get_clerk_runtime", get_clerk_runtime)
        monkeypatch.setattr(budget_deploy, "get_bot_task_registry", lambda: registry)

        after = await budget_deploy.account_money_view(repo.account_id)
    finally:
        await close_synthetic_clerk_runtimes()
    assert after == before
    assert all(segment.strategy_instance_id != sid for segment in after.segments)


async def test_shadow_draws_its_bar_from_its_own_shadow_pool(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="shadow:LIVE-1", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
        reading = AccountObservation(observed_at_ms=NOON, broker_cash_usd=5000, cash_available_usd=2500,
                                     last_equity_usd=2500, position_count=0, equity_usd=2500)
        runtime = SimpleNamespace(selected_account_id=repo.account_id, account_authority_kind="shadow", sqlite_repository=repo,
                                  envelope_sync=_Sync(LiveEnvelopeGate(values=None, custody_is_simulated=True), reading))
        monkeypatch.setattr(budget_deploy, "get_active_clerk_runtime", lambda: runtime)

        view = await budget_deploy.account_money_view("LIVE-1")
    finally:
        repo.close()
    # The simulated pool's own cash, never the real account's $5,000.
    assert view.state == "ready" and view.world == "shadow" and view.account_id == "LIVE-1"
    assert _segments(view) == [("free", "free to deploy", "2500.00", 10_000)]


async def test_bot_budget_read_carries_the_same_shaded_parts_as_its_segment(authority: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, runtime, _ = _committed_view(authority)
    segment = (await budget_deploy.account_money_view(repo.account_id)).segments[0]
    calls: list[int] = []
    real = budget_deploy.current_risk_readiness
    monkeypatch.setattr(budget_deploy, "current_risk_readiness", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    runtime.envelope_sync.envelope.withdraw()

    view = budget_deploy._budget_view(runtime, "view")

    assert view.state == "ready" and view.parts == segment.parts
    # Review A5: one readiness judgement per read, even without admission.
    assert len(calls) == 1


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
    assert "Settings" in other.json()["detail"]["why"]


async def test_money_that_does_not_add_up_is_withheld_loudly_and_deploy_still_previews(
    authority: tuple, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from app.broker.alpaca.clerk.account_money import MoneyConservationError

    repo, _, _ = _committed_view(authority)

    def unconserved(*args, **kwargs):
        raise MoneyConservationError("parts 1 != total 2")

    monkeypatch.setattr(budget_deploy, "money_bar", unconserved)

    view = await budget_deploy.account_money_view(repo.account_id)
    preview = budget_deploy.preview_budget(repo.account_id, _request(), resolved_parameters={})

    assert view.state == "unavailable" and "does not add up" in view.detail and view.segments == ()
    assert view.equity_usd == "1000.00"
    assert any(getattr(record, "action", None) == "account_money_unconserved" for record in caplog.records)
    assert preview.state == "ready" and preview.money_after is not None and preview.money_after.state == "unavailable"
