"""Trader money review uses custody facts and immutable consent, not UI math."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate, observation_is_fresh
from app.broker.alpaca.clerk.money import MoneyInputError
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity
from app.schemas.broker_bots import AlpacaPaperDeployRequest
from app.schemas.deployment_budget import (
    AccountMoneyView,
    BudgetDeployCommandReceipt,
    DeploymentBudgetInput,
    DeploymentBudgetPreview,
    DeploymentBudgetView,
    MoneyParts,
    MoneySegment,
)
from app.schemas.exit_terms import ExitTermsInput
from app.schemas.market_liveness import MarketStatusSnapshot, MarketStatusSource, TopOfBookQuote
from app.services import market_liveness
from app.services.broker_v2_panel import bot_custody, budget_deploy, sqlite_roster_status
from app.services.broker_v2_panel.deploy_submissions import DeploySubmission
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock


@pytest.mark.parametrize(("model", "field", "payload"), [
    (DeploymentBudgetPreview, "observed_at_ms", dict(state="ready", detail="Ready", world="real_paper", custody_account_id="PAPER")),
    (DeploymentBudgetView, "observed_at_ms", dict(state="ready", headline="Ready", detail="Ready", world="real_paper", strategy_instance_id="bot")),
    (BudgetDeployCommandReceipt, "recorded_at_ms", dict(
        status="pending", outcome="pending", receipt_id="receipt", command_id="command", strategy_instance_id="bot",
        run_id="run", account_id="PAPER", world="real_paper", committed_usd="100.00", message="Pending",
        explanation="Pending", next_action="Refresh", first_deployed_at_ms=0,
    )),
    (BudgetDeployCommandReceipt, "first_deployed_at_ms", dict(
        status="pending", outcome="pending", receipt_id="receipt", command_id="command", strategy_instance_id="bot",
        run_id="run", account_id="PAPER", world="real_paper", committed_usd="100.00", message="Pending",
        explanation="Pending", next_action="Refresh", recorded_at_ms=0,
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
        strategy_key="deployment_validation", symbol=symbol,
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
    monkeypatch.setattr(bot_custody, "get_active_clerk_runtime", lambda: runtime)
    monkeypatch.setattr(bot_custody, "get_bot_task_registry", lambda: None)
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
    # H18: simulated cash is the bot's own; no real account's number or cash applies.
    assert preview.custody_account_id is None and preview.unreserved_usd is None
    assert [item.key for item in preview.shortcuts] == ["position_headroom"]
    assert preview.budget_usd == "2000.00"


def _deployed_bot(repo: ClerkSqliteRepository, observation: AccountObservation, sid: str, symbol: str, *,
                  mode: str = "trade") -> None:
    """A bot Deploy committed and started on ``symbol`` in this account."""
    terms = _request(symbol).exit_terms.seal()
    repo.register_strategy_instance(
        strategy_instance_id=sid, symbol=symbol, config_hash=f"seal-{sid}", exit_terms=terms,
        strategy_key="deployment_validation", display_name=sid,
        config_json=canonicalize({"mode": mode, "carryover_policy": "FORBID", "quantity": 1}),
    )
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    gate.publish(observation)
    submit_budgeted_deploy(
        repo, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}", world="real_paper", committed_cents=20_000,
        configuration_hash=f"seal-{sid}", exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")),
        risk_revision=1, actor="owner", envelope=gate, minimum_position_cost=Decimal(100),
    )


def test_the_review_names_the_other_bots_already_trading_the_symbol(
    authority, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """#2622: Deploy warns, in one short line, when another bot in this account already trades the symbol.

    Alpaca refuses an order that could trade against another open order in
    the account, so two bots on one symbol can refuse each other's orders.
    Only bots that may trade this account's money on that symbol are named:
    never one on another symbol, a Dry Run, or one that has finished. A Dry
    Run's own Deploy never trades the account, so it is never warned. The
    line fits the Confirm step (#2581): one bot by name, the rest counted.
    """
    repo, _, snapshot = authority
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: tmp_path)
    for sid, symbol, mode in (("spy-a", "SPY", "trade"), ("qqq-a", "QQQ", "trade"),
                              ("spy-sim", "SPY", "dry_run"), ("spy-done", "SPY", "trade")):
        _deployed_bot(repo, snapshot.observation, sid, symbol, mode=mode)
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="spy-done",
                    lifecycle_run_id="run-spy-done", clock=repo.clock)

    assert budget_deploy.same_symbol_note("BUDGET-PAPER", _request("SPY")) == "Also traded here by spy-a."
    assert budget_deploy.same_symbol_note("BUDGET-PAPER", _request("NVDA")) is None
    assert budget_deploy.same_symbol_note("BUDGET-PAPER", _request("SPY", execution_mode="dry_run")) is None

    for sid in ("spy-b", "spy-c"):
        _deployed_bot(repo, snapshot.observation, sid, "SPY")

    assert budget_deploy.same_symbol_note("BUDGET-PAPER", _request("SPY")) == "Also traded here by spy-a +2 more."


def test_the_review_names_a_bot_on_the_symbol_whose_lifecycle_cannot_be_read(
    authority, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Home cannot place a bot whose configuration it cannot read, so the Clerk cannot say it is finished: it may still trade."""
    repo, _, snapshot = authority
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: tmp_path)
    _deployed_bot(repo, snapshot.observation, "spy-unread", "SPY", mode="not-a-mode")

    assert budget_deploy.same_symbol_note("BUDGET-PAPER", _request("SPY")) == "Also traded here by spy-unread."


@pytest.mark.parametrize("failure", [
    sqlite3.OperationalError("database is locked"),
    MoneyInputError("Money evidence is not finite."),
])
def test_a_roster_that_cannot_be_read_omits_the_warning_and_never_fails_the_review(
    authority, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Exception,
) -> None:
    """The roster is read only for the warning: its failure is logged, and the money review goes on without it."""
    def unreadable(repository: ClerkSqliteRepository, *, world: str) -> list:
        raise failure

    monkeypatch.setattr(budget_deploy, "home_roster", unreadable)

    with caplog.at_level(logging.WARNING, logger="app.services.broker_v2_panel.budget_deploy"):
        assert budget_deploy.same_symbol_note("BUDGET-PAPER", _request("SPY")) is None

    [logged] = [record for record in caplog.records if getattr(record, "action", None) == "deploy_same_symbol_note_unavailable"]
    assert logged.exc_info is not None


_CLAIMED_AT = 1


def _submitted(sid: str) -> DeploySubmission:
    """The ledger claim a Deploy of ``sid`` would hold."""
    return DeploySubmission(submission_key="submission-0001", strategy_instance_id=sid, claimed_at_ms=_CLAIMED_AT, request_fingerprint="f")


async def test_recovery_returns_committed_outcome_even_after_evidence_expires(authority) -> None:
    repo, _, snapshot = authority
    request = _request(budget=DeploymentBudgetInput(amount_usd="500", risk_revision=1))
    terms = request.exit_terms.seal()
    repo.register_strategy_instance(strategy_instance_id="review-a", symbol="SPY", config_hash="seal", exit_terms=terms)
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    gate.publish(snapshot.observation)
    submit_budgeted_deploy(repo, strategy_instance_id="review-a", lifecycle_run_id="run", world="real_paper", committed_cents=50_000, configuration_hash="seal", exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")), risk_revision=1, actor="owner", envelope=gate, minimum_position_cost=Decimal(100), request_fingerprint=budget_deploy.request_fingerprint(request, custody_account_id=repo.account_id, world="real_paper"))
    snapshot.observation = None
    receipt = await budget_deploy.command_receipt(repo.account_id, _submitted("review-a"))
    assert receipt.status == "pending" and receipt.committed_usd == "500.00"
    # The first-deploy instant is the custody commit's, never the name claim's.
    assert receipt.first_deployed_at_ms == repo.deployment_budget("review-a")["committed_at_ms"] != _CLAIMED_AT
    # H12: every read says what is recorded, never that a result is "returned unchanged".
    assert receipt.message == "review-a is committed; its launch is not confirmed yet"
    assert "unchanged" not in receipt.explanation and "$500.00 is set aside" in receipt.explanation


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


def test_a_disconnected_feed_is_unavailable_with_its_own_copy_not_a_price_wait(
    authority: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2559: with the IBKR stream down, no quote can arrive on any re-check, so
    the preview must not promise one. The state and the copy both differ from
    a connected feed's first-quote wait."""
    store = market_liveness.get_market_liveness_store()
    _publish_book()
    store.mark_stream_disconnected(observed_at_ms=NOON)
    monkeypatch.setattr("app.utils.timestamps.now_ms_utc", lambda: NOON)

    down = budget_deploy.preview_budget("BUDGET-PAPER", _request(symbol="NVDA"), resolved_parameters={})

    assert down.state == "unavailable"
    assert "feed is unavailable" in down.detail
    assert "fresh IBKR price" not in down.detail
    assert down.review_token is None and down.shortcuts == ()


def test_fractional_cent_consent_is_rejected_at_wire_boundary() -> None:
    with pytest.raises(ValidationError):
        DeploymentBudgetInput(amount_usd="100.001", risk_revision=0)


def _deploy_bot(repo: ClerkSqliteRepository, gate: LiveEnvelopeGate, sid: str, *, cents: int) -> None:
    """Register and Deploy a one-share SPY bot whose run is ``<sid>-run``."""
    terms = _request().exit_terms.seal()
    config = {"symbol": "SPY", "quantity": 1}
    from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
    repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash=canonical_sha256(config),
        config_json=canonicalize(config), exit_terms=terms)
    submit_budgeted_deploy(repo, strategy_instance_id=sid, lifecycle_run_id=f"{sid}-run", world="real_paper",
        committed_cents=cents, configuration_hash=canonical_sha256(config), exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")),
        risk_revision=1, actor="owner", envelope=gate, minimum_position_cost=Decimal("100.02"))


def _committed_view(authority: tuple) -> tuple:
    repo, runtime, snapshot = authority
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    gate.publish(snapshot.observation)
    runtime.envelope_sync.envelope = gate
    _deploy_bot(repo, gate, "view", cents=20_000)
    return repo, runtime, gate


def test_budget_read_uses_the_sealed_next_position_and_current_cash(authority: tuple) -> None:
    repo, runtime, gate = _committed_view(authority)
    assert budget_deploy._budget_view(runtime, "view").entry_eligible
    before = repo.custody_transitions()
    _publish_book(("SPY", 200))
    view = budget_deploy._budget_view(runtime, "view")
    assert not view.entry_eligible and "200.01 USD" in view.detail
    assert ("Free to trade", "200.00", False) in _statement(view)
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

    #2559: the recovery GET is now a read -- it appends no custody
    transition and the command stays pending -- and a restart's boot
    restoration is what releases the orphan, after which the receipt reads
    failed."""
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
        primary = SimpleNamespace(sqlite_repository=primary_repo)
        monkeypatch.setattr(budget_deploy, "_primary", lambda account: primary)
        monkeypatch.setattr(bot_custody, "get_active_clerk_runtime", lambda: primary)
        monkeypatch.setattr(bot_custody, "get_bot_task_registry", lambda: recovered)

        # The orphan's own durable evidence, read without writing it: count
        # the private store's custody transitions around the GET.
        sim_repo = ClerkSqliteRepository.open(
            account_id=f"sim:{sid}", artifacts_root=tmp_path, clock=clock,
        )
        transitions_before = len(sim_repo.custody_transitions())
        sim_repo.close()

        receipt = await budget_deploy.command_receipt("PARENT", _submitted(sid))

        assert receipt is not None
        assert receipt.status == "pending" and receipt.world == "synthetic" and receipt.committed_usd == "500.00"
        assert receipt.run_id == f"{sid}:run-1"
        # A read composes nothing that outlives it and records no binding.
        assert get_clerk_runtime(f"sim:{sid}") is None
        assert recovered.bindings_for_broker("alpaca") == []
        # A read appends no custody transition (#2559): the orphan's release
        # belongs to boot's restoration, not to whoever happens to ask.
        sim_repo = ClerkSqliteRepository.open(
            account_id=f"sim:{sid}", artifacts_root=tmp_path, clock=clock,
        )
        try:
            assert len(sim_repo.custody_transitions()) == transitions_before
            assert sim_repo.active_run(sid) is not None  # still orphaned, still pending
        finally:
            sim_repo.close()

        # The restart's boot restoration releases the orphan through the same
        # recovery path bound Dry Runs use; the receipt then reads failed.
        await recovered.start_dry_run_restoration()
        receipt = await budget_deploy.command_receipt("PARENT", _submitted(sid))
        assert receipt is not None
        assert receipt.status == "failed"
        # An identity no private authority ever held still reads the primary.
        assert await budget_deploy.command_receipt("PARENT", _submitted("never-deployed")) is None
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
    assert (view.settling_usd, view.account_shortfall_usd, view.stopped_holding_count) == ("0.00", None, 0)
    assert (view.equity_usd, view.today_pnl_usd, view.open_pnl_usd, view.open_pnl_detail) == ("1000.00", "0.00", "0.00", None)
    parts = view.segments[0].parts
    assert parts is not None and (parts.in_shares_usd, parts.pending_usd, parts.free_usd, parts.free_bps) == ("0.00", "0.00", "200.00", 10_000)
    assert view.segments[0].shortfall_usd is None and view.observed_at_ms == NOON
    assert (view.segments[0].palette_index, view.segments[1].palette_index) == (0, None)
    assert view.detail == "Cash plus shares at the price paid."
    # Deploy would admit a new bot: nothing refuses beside the bar.
    assert view.deploy_refusal is None


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
    assert view.deploy_refusal is not None


async def test_a_real_accounts_today_is_the_exact_day_of_its_recorded_figures(authority: tuple) -> None:
    """#2586, owner decision 2026-09-29: exact for all accounts, not only simulated ones.

    Alpaca reports equity 10004.015 over a 10000.70 prior close, with deposits
    of 1.10 and 2.20 since that close: today is exactly $0.015, shown $0.02
    (half-even). The float arithmetic on the same figures, 0.014999999998690061,
    showed $0.01.
    """
    repo, runtime, _ = _committed_view(authority)
    deposits = tuple(
        BrokerActivity(broker="alpaca", activity_id=f"deposit-{amount}", activity_type="CSD", category="non_trade_activity",
                       symbol=None, side=None, quantity=None, price=None, net_amount=amount, occurred_at_ms=NOON, observed_at_ms=NOON)
        for amount in (1.1, 2.2)
    )
    runtime.envelope_sync.reading = replace(
        runtime.envelope_sync.reading, equity_usd=10_004.015, last_equity_usd=10_000.7, risk_cash_flows=deposits)

    view = await budget_deploy.account_money_view(repo.account_id)

    assert (view.equity_usd, view.today_pnl_usd) == ("10004.02", "0.02")


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


def test_a_shortfall_is_stated_only_where_something_is_short() -> None:
    """A legend never reads "short of next entry $0.00": no shortfall is absent."""
    from app.broker.alpaca.clerk.account_money import account_money, money_bar
    from app.broker.alpaca.clerk.budgets import account_budget, deployment_budget

    def bot(sid: str, position: str) -> object:
        return deployment_budget(strategy_instance_id=sid, committed_cents=100_000, active=True, realized_gross="0",
                                 fees=Decimal(0), position_cost=Decimal(position), pending_orders=Decimal(0))

    # "slipped" filled $5.01 beyond its $1,000; "steady" is within its budget.
    budget = account_budget(cash="7990", deployments=[bot("slipped", "1005.01"), bot("steady", "764.71")],
                            order_claims=Decimal(0), fee_claims=Decimal(0))
    bar = money_bar(account_money(budget, unseen_fills=Decimal(0), unseen_sales=Decimal(0), holdings=(),
                                  registration_order=("slipped", "steady")))
    slipped, steady = (budget_deploy._segment_view(segment) for segment in bar.segments[:2])

    assert (slipped.strategy_instance_id, slipped.shortfall_usd) == ("slipped", "5.01")
    assert (steady.strategy_instance_id, steady.shortfall_usd) == ("steady", None)
    with pytest.raises(ValidationError, match="only when something is short"):
        MoneySegment.model_validate({**steady.model_dump(), "shortfall_usd": "0.00"})
    with pytest.raises(ValidationError, match="only when the account is overdrawn"):
        AccountMoneyView.model_validate({**_ready_money_view().model_dump(), "account_shortfall_usd": "0.00"})


def test_a_stated_figure_is_whole_cents_never_a_truncated_one() -> None:
    """A sub-cent or non-dollar string is refused, not floor-ed into a figure."""
    overdrawn = {**_ready_money_view().model_dump(), "segments": [
        {**_ready_money_view().segments[0].model_dump(), "amount_usd": "0.005", "share_bps": 10_000}]}
    with pytest.raises(ValidationError, match=r"not whole cents: '0\.005'"):
        AccountMoneyView.model_validate(overdrawn)
    with pytest.raises(ValidationError, match=r"not whole cents: 'NaN'"):
        MoneyParts.model_validate({"in_shares_usd": "NaN", "in_shares_bps": 10_000,
                                   "pending_usd": "0.00", "pending_bps": 0, "free_usd": "0.00", "free_bps": 0})
    with pytest.raises(ValidationError, match=r"not a dollar amount: 'abc'"):
        MoneyParts.model_validate({"in_shares_usd": "abc", "in_shares_bps": 10_000,
                                   "pending_usd": "0.00", "pending_bps": 0, "free_usd": "0.00", "free_bps": 0})


def _ready_money_view() -> AccountMoneyView:
    return AccountMoneyView(
        state="ready", detail="Cash plus shares at the price paid.", world="real_paper", account_id="PA1",
        observed_at_ms=NOON, total_usd="1.00", cash_usd="1.00", free_to_deploy_usd="1.00", in_bots_usd="0.00",
        held_by_stopped_usd="0.00", outside_bots_usd="0.00", account_charges_usd="0.00", settling_usd="0.00",
        stopped_holding_count=0, open_pnl_usd="0.00",
        segments=(MoneySegment(kind="free", label="free to deploy", amount_usd="1.00", share_bps=10_000),),
    )


async def test_open_pnl_is_canonical_fifo_or_withheld_with_its_reason(authority: tuple) -> None:
    """Review A4: never Alpaca's equity less the bar's cost total."""
    repo, runtime, gate = _committed_view(authority)
    _hold_one_share(repo, gate)
    # Alpaca marks the share $5 up: the retired formula would have said $5.00.
    runtime.envelope_sync.reading = replace(runtime.envelope_sync.reading, equity_usd=1005)

    real = await budget_deploy.account_money_view(repo.account_id)
    assert real.state == "ready" and real.total_usd == "1000.00" and real.equity_usd == "1005.00"
    assert real.open_pnl_usd is None and real.open_pnl_detail == budget_deploy._OPEN_PNL_UNPRICED

    # A simulated reading values its own lots with its own marks (fifo.exact_open_pnl).
    runtime.envelope_sync.reading = replace(runtime.envelope_sync.reading, simulation_session_start_ms=NOON - 1, unrealized_pl_usd=Decimal("4.2"))
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
    # Slice 1 review condition (#2562): free to deploy must not look
    # spendable while Deploy refuses, so the money read carries the refusal
    # in the preview's own words, for Home to state beside the bar.
    assert view.deploy_refusal == preview.detail
    assert "daily loss limit" in view.deploy_refusal


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
        monkeypatch.setattr(bot_custody, "get_bot_task_registry", lambda: registry)

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


async def test_bot_budget_read_carries_the_same_slice_home_draws(authority: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, runtime, _ = _committed_view(authority)
    segment = (await budget_deploy.account_money_view(repo.account_id)).segments[0]
    calls: list[int] = []
    real = budget_deploy.current_risk_readiness
    monkeypatch.setattr(budget_deploy, "current_risk_readiness", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    runtime.envelope_sync.envelope.withdraw()

    view = budget_deploy._budget_view(runtime, "view")

    # Review A2/B1: the bot page draws Home's own slice, only widened to fill its bar.
    assert view.state == "ready" and view.segment == segment.model_copy(update={"share_bps": 10_000})
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


@pytest.mark.parametrize("to_http", [budget_deploy.budget_error, budget_deploy.money_error], ids=["deploy", "money"])
def test_a_money_refusal_carries_its_reason_code_as_well_as_its_words(to_http) -> None:
    """#2553: an unknown fee is refused under its own code, and the HTTP error names it, not only its message."""
    from app.broker.alpaca.clerk.sqlite.envelope_reservations import EntryFeeProvisionUnrecorded

    error = to_http(EntryFeeProvisionUnrecorded("An earlier entry order has no recorded fee estimate."))

    assert error.reason_code == "ENTRY_FEE_PROVISION_UNRECORDED"
    assert error.detail == "An earlier entry order has no recorded fee estimate."
    assert to_http(BudgetUnavailable("Wait for this account's custody recovery to finish.")).reason_code is None


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
    runtime.envelope_sync.reading = replace(gate.latest_observation(), observed_at_ms=repo.clock(), cash_available_usd=900)

    view = budget_deploy._budget_view(runtime, "view")

    assert view.state == "ready" and not view.entry_eligible
    assert view.headline == "Stopped · still holds shares"
    assert view.detail == ("It released $100.00 when it stopped; $100.00 stayed claimed. "
                           "The money in its shares comes back when they are sold.")
    assert _statement(view) == [
        ("Budget set aside at deploy", "200.00", False),
        ("Realized gains and losses", "0.00", False),
        ("Fees", "0.00", False),
        ("Balance", "200.00", True),
        ("Released at stop", "100.00", False),
        ("Still in shares, at cost", "100.00", False),
        ("Still in entry orders", "0.00", False),
    ]
    assert view.note is None
    assert view.segment is not None and (view.segment.kind, view.segment.amount_usd, view.segment.released_usd) == (
        "stopped", "100.00", "100.00",
    )


def _cents(amount_usd: str) -> int:
    return int(Decimal(amount_usd) * 100)


def test_a_stopped_bots_lines_add_up_to_its_balance_when_a_fee_is_fractional(authority: tuple) -> None:
    """Review A1: rounded on their own, the stopped lines missed Balance by a
    cent whenever a fee was fractional, and named fee cash the account owes as
    the bot's money "waiting"."""
    from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.contract.models import BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

    repo, runtime, gate = _committed_view(authority)
    accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="view", decision_id="held",
                           lifecycle_run_id="view-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
                           reference_price=100, envelope=gate)
    _append_slice(repo, accepted, execution_id="held-fill", quantity=1, price=100.005, source_event_at_ms=NOON, fee=0.0049)
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="view", lifecycle_run_id="view-run", clock=repo.clock)
    repo.clock.advance(10_000)
    runtime.envelope_sync.reading = replace(gate.latest_observation(), observed_at_ms=repo.clock(), cash_available_usd=899.9901)

    view = budget_deploy._budget_view(runtime, "view")

    lines = dict((label, amount) for label, amount, _ in _statement(view))
    assert (lines["Balance"], lines["Released at stop"], lines["Still in shares, at cost"], lines["Still in entry orders"]) == (
        "200.00", "100.00", "100.00", "0.00",
    )
    held = ("Released at stop", "Still in shares, at cost", "Still in entry orders")
    assert sum(_cents(lines[label]) for label in held) == _cents(lines["Balance"])
    assert "Over its budget by" not in lines
    assert view.segment is not None and view.segment.released_usd == lines["Released at stop"]


# ── A stopped bot's release is the fact its Stop recorded (#2555) ───────────


def _stop(repo: ClerkSqliteRepository, sid: str = "view") -> None:
    from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run

    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id=sid, lifecycle_run_id=f"{sid}-run", clock=repo.clock)


def _read_view(runtime: SimpleNamespace, gate: LiveEnvelopeGate, *, cash: float) -> DeploymentBudgetView:
    """This bot's money read against a fresh account reading holding ``cash``."""
    from app.broker.alpaca.clerk.sqlite.day_pnl import risk_fill_sequence

    repo = runtime.sqlite_repository
    reading = replace(gate.latest_observation(), observed_at_ms=repo.clock(), cash_available_usd=cash,
                      risk_fill_sequence=risk_fill_sequence(repo))
    gate.publish(reading)
    runtime.envelope_sync.reading = reading
    view = budget_deploy._budget_view(runtime, "view")
    assert view.state == "ready", view.detail
    return view


def _lines(view: DeploymentBudgetView) -> dict[str, str]:
    return {label: amount for label, amount, _ in _statement(view)}


def test_a_stopped_bots_released_money_stays_what_its_stop_released(authority: tuple) -> None:
    """#2555: "released" was re-derived from the stopped bot's money on every
    read, so a fee posting after Stop -- and another bot trading on the same
    fee day, which re-splits that day's fees -- moved a one-time release."""
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.contract.models import BrokerActivity, BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.conftest import YESTERDAY_NOON
    from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

    repo, runtime, gate = _committed_view(authority)
    held = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="view", decision_id="held",
                        lifecycle_run_id="view-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
                        reference_price=100, envelope=gate)
    # Filled last session with its fee not reported: the bot carries the modelled fee.
    _append_slice(repo, held, execution_id="held-fill", quantity=1, price=100, source_event_at_ms=YESTERDAY_NOON)
    _stop(repo)
    repo.clock.advance(10_000)
    at_stop = _read_view(runtime, gate, cash=900)

    # After Stop another bot trades on that fee day, and the day's fee posts.
    _deploy_bot(repo, gate, "other", cents=30_000)
    bought = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="other", decision_id="bought",
                          lifecycle_run_id="other-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
                          reference_price=100, envelope=gate)
    _append_slice(repo, bought, execution_id="other-fill", quantity=2, price=100, source_event_at_ms=YESTERDAY_NOON + 1)
    repo.clock.advance(10_000)
    posted = BrokerActivity(broker="alpaca", activity_id="day-fee", activity_type="FEE", category="non_trade_activity",
                            symbol=None, side=None, quantity=None, price=None, net_amount=-0.05,
                            occurred_at_ms=YESTERDAY_NOON, observed_at_ms=repo.clock())
    assert record_fee_evidence(repo, [posted], checked_at_ms=repo.clock(), history_complete=True)
    later = _read_view(runtime, gate, cash=700)

    assert _statement(at_stop) == [
        ("Budget set aside at deploy", "200.00", False),
        ("Realized gains and losses", "0.00", False),
        ("Fees", "-0.01", False),
        ("Balance", "199.99", True),
        ("Released at stop", "99.99", False),
        ("Still in shares, at cost", "100.00", False),
        ("Still in entry orders", "0.00", False),
    ]
    # The day's posted fee came to a cent more than the one modelled at Stop:
    # a charge after Stop, on its own signed line -- never a change to the
    # release, and never an overrun while its shares are inside its balance.
    assert _statement(later) == [
        ("Budget set aside at deploy", "200.00", False),
        ("Realized gains and losses", "0.00", False),
        ("Fees", "-0.02", False),
        ("Balance", "199.98", True),
        ("Released at stop", "99.99", False),
        ("Charged since it stopped", "-0.01", False),
        ("Still in shares, at cost", "100.00", False),
        ("Still in entry orders", "0.00", False),
    ]
    assert at_stop.segment is not None and later.segment is not None
    assert later.segment.released_usd == at_stop.segment.released_usd == "99.99"
    assert not later.segment.released_estimated


def test_a_bot_over_its_budget_when_it_stopped_reads_as_over_not_as_charged_since(authority: tuple) -> None:
    """#2555 review: the overrun a bot carried into its Stop is named once, as
    the running bot names it -- shares beyond the balance -- and nothing reads
    as charged or come back since the Stop while its money has not moved."""
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.contract.models import BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

    repo, runtime, gate = _committed_view(authority)
    held = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="view", decision_id="held",
                        lifecycle_run_id="view-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
                        reference_price=100, envelope=gate)
    # The market filled it well above its reference: $201 of shares on a $200 budget.
    _append_slice(repo, held, execution_id="held-fill", quantity=1, price=201, source_event_at_ms=NOON, fee=0)
    _stop(repo)
    repo.clock.advance(10_000)

    view = _read_view(runtime, gate, cash=799)

    assert _statement(view) == [
        ("Budget set aside at deploy", "200.00", False),
        ("Realized gains and losses", "0.00", False),
        ("Fees", "0.00", False),
        ("Balance", "200.00", True),
        ("Released at stop", "0.00", False),
        ("Still in shares, at cost", "201.00", False),
        ("Still in entry orders", "0.00", False),
        ("Over its budget by", "1.00", False),
    ]


def test_money_a_stopped_bot_gets_back_after_stop_is_never_counted_as_released(authority: tuple) -> None:
    """#2555: shares sold after Stop come back as their own line; the released
    figure stays the Stop's, and every line still adds up to the balance."""
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.contract.models import BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.test_budget_claims import _record_sale
    from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

    repo, runtime, gate = _committed_view(authority)
    held = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="view", decision_id="held",
                        lifecycle_run_id="view-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
                        reference_price=100, envelope=gate)
    _append_slice(repo, held, execution_id="held-fill", quantity=1, price=100, source_event_at_ms=NOON, fee=0)
    _stop(repo)
    repo.clock.advance(5_000)
    _record_sale(repo, held, key="sold-after-stop", price=110, at_ms=repo.clock())
    repo.clock.advance(5_000)

    view = _read_view(runtime, gate, cash=1010)

    assert view.headline == "Stopped · finished" and view.segment is None
    assert _statement(view) == [
        ("Budget set aside at deploy", "200.00", False),
        ("Realized gains and losses", "10.00", False),
        ("Fees", "0.00", False),
        ("Balance", "210.00", True),
        ("Released at stop", "100.00", False),
        ("Came back since it stopped", "110.00", False),
        ("Still in shares, at cost", "0.00", False),
        ("Still in entry orders", "0.00", False),
    ]


def test_a_stopped_bots_still_claimed_money_shrinks_only_as_its_own_order_settles(authority: tuple) -> None:
    """#2555: an entry order still working at Stop stays claimed while other
    money moves, and leaves the claim only when that order itself ends -- as
    money that came back, never as released."""
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
    from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

    repo, runtime, gate = _committed_view(authority)
    working = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="view", decision_id="working",
                           lifecycle_run_id="view-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
                           reference_price=100, envelope=gate)
    assert working.effect_operation_id is not None and working.order_ref is not None
    _stop(repo)
    at_stop = _lines(_read_view(runtime, gate, cash=1000))
    assert at_stop["Released at stop"] == "99.99" and at_stop["Still in entry orders"] == "100.01"

    # Other money moves: another bot trades and the account's cash changes.
    _deploy_bot(repo, gate, "other", cents=30_000)
    bought = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="other", decision_id="bought",
                          lifecycle_run_id="other-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
                          reference_price=100, envelope=gate)
    _append_slice(repo, bought, execution_id="other-fill", quantity=2, price=100, source_event_at_ms=NOON, fee=0)
    repo.clock.advance(5_000)
    moved = _lines(_read_view(runtime, gate, cash=800))
    assert (moved["Released at stop"], moved["Still in entry orders"]) == ("99.99", "100.01")
    assert "Came back since it stopped" not in moved

    # Its own order ends unfilled: only now does its claim settle.
    fold_order_evidence(repo, effect_operation_id=working.effect_operation_id, order=BrokerOrder(
        broker="alpaca", order_id="bo-working", client_order_id=working.order_ref, symbol="SPY", asset_class="us_equity",
        side="buy", order_type="market", time_in_force="day", quantity=1, filled_quantity=0, limit_price=None,
        stop_price=None, filled_avg_price=None, status="canceled", submitted_at_ms=NOON, created_at_ms=NOON,
        updated_at_ms=repo.clock(), filled_at_ms=None, canceled_at_ms=repo.clock(), expired_at_ms=None, events=[],
        observed_at_ms=repo.clock(),
    ))
    settled = _read_view(runtime, gate, cash=800)
    assert (_lines(settled)["Balance"], _lines(settled)["Released at stop"]) == ("200.00", "99.99")
    assert (_lines(settled)["Came back since it stopped"], _lines(settled)["Still in entry orders"]) == ("100.01", "0.00")
    # What the Stop released and what stayed claimed then are the Stop's own figures.
    assert settled.detail.startswith("It released $99.99 when it stopped; $100.01 stayed claimed.")


def test_a_bot_whose_stop_recorded_no_release_shows_an_estimate_so_labelled(
    authority: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2555: a Stop that recorded no release -- every Stop before #2555, or
    one whose money could not be valued at that instant -- still renders, as
    an estimate from the bot's money now and labelled so, never as a fact.
    Its copy says only what is true of both: the Stop recorded no amount."""
    from app.broker.alpaca.clerk.sqlite import budget_projection
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.contract.models import BrokerOrderLeg
    from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

    repo, runtime, gate = _committed_view(authority)
    held = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="view", decision_id="held",
                        lifecycle_run_id="view-run", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
                        reference_price=100, envelope=gate)
    _append_slice(repo, held, execution_id="held-fill", quantity=1, price=100, source_event_at_ms=NOON, fee=0)

    def unvaluable(*_args: object, **_kwargs: object) -> None:
        raise BudgetUnavailable("Fee evidence is unresolved.")

    with monkeypatch.context() as patched:
        patched.setattr(budget_projection, "value_release", unvaluable)
        _stop(repo)
    stop = next(row for row in repo.custody_transitions() if row["transition_kind"] == "RUN_STOPPED")
    assert "released_cents" not in stop["facts_json"]
    repo.clock.advance(10_000)

    view = _read_view(runtime, gate, cash=900)

    assert _statement(view) == [
        ("Budget set aside at deploy", "200.00", False),
        ("Realized gains and losses", "0.00", False),
        ("Fees", "0.00", False),
        ("Balance", "200.00", True),
        ("Released at stop (estimate)", "100.00", False),
        ("Still in shares, at cost", "100.00", False),
        ("Still in entry orders", "0.00", False),
    ]
    assert view.detail == ("Its Stop did not record what it released, so that figure is an estimate from its money now. "
                           "The money in its shares comes back when they are sold.")
    assert view.segment is not None and (view.segment.released_usd, view.segment.released_estimated) == ("100.00", True)
