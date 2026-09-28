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
