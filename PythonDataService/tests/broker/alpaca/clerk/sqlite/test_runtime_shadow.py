"""The Clerk runs on the shadow authority: every synthesized fill is bound to the decision bar (ADR 0059 D2)."""

from __future__ import annotations

from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.services.source_bar_ledger import SourceBarLedger
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, _FakeTradePort
from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import (
    _EXTENDED_POLICY,
    _binding,
    _closed_clock,
    _stream_health,
)
from tests.broker.alpaca.clerk.test_shadow_broker import _LiveRead, _retain

ACCOUNT_ID = "shadow:9LIVE0001"
SID = "spy-bot"
RUN_ID = "run-1"


def test_shadow_facade_requires_a_shadow_id_and_a_live_account(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        with pytest.raises(AccountAuthorityIdentityError, match="reads a live account"):
            SqliteAlpacaClerkFacade(
                repo=repo,
                read=_FakeReadPort(),
                trade=_FakeTradePort(),
                authority_kind="shadow",
                account_mode="paper",
            )
    finally:
        repo.close()
    paper = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    try:
        with pytest.raises(AccountAuthorityIdentityError, match="shadow: account identity"):
            SqliteAlpacaClerkFacade(
                repo=paper,
                read=_FakeReadPort(),
                trade=_FakeTradePort(),
                authority_kind="shadow",
                account_mode="live",
            )
    finally:
        paper.close()


async def test_enter_on_the_shadow_authority_is_synthesized_from_the_bound_decision_bar(
    tmp_path: Path,
) -> None:
    ports = compose_shadow_ports(
        live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path
    )
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id="shadow-evidence:spy-bot")
    decision = _retain(evidence, minute=600, close="100.25")
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=ports.read,
        trade=ports.trade,
        authority_kind="shadow",
        account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
    )
    binding = _binding(use_rth=True).model_copy(update={"sealed_account_id": ACCOUNT_ID})
    await facade.register_strategy_run(binding)
    try:
        unproven = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-0",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=True,
            retained_source_bar=None,
        )
        assert unproven.state == "rejected" and "SIMULATED_SOURCE_BAR_UNPROVEN" in unproven.explanation

        receipt = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-1",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=True,
            retained_source_bar=decision,
        )
        # Acknowledged, not yet accounted: a submit response is never fill
        # math on any authority (``fold_order_submission_acknowledgement``),
        # so the synthesized fill below is still the sweep's to fold.
        assert receipt.state == "submitted", receipt.explanation
        [order] = await ports.read.list_orders()
        assert (order.status, order.filled_avg_price, order.filled_at_ms) == (
            "filled",
            100.25,
            decision.end_ms,
        )
        assert order.client_order_id is not None
        assert order.client_order_id.startswith("learn-ai/spy-bot/v1:")
        assert [position.symbol for position in await ports.read.list_positions()] == ["SPY"]
    finally:
        repo.close()
        evidence.close()


async def _extended_enter_on_a_closed_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream_health: StreamHealthGate | None,
) -> tuple[EffectOperationState, str]:
    """Drive one POST-session shadow ENTER while the broker clock reports CLOSED."""
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _closed_clock)
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id="shadow-evidence:spy-bot")
    decision = _retain(evidence, minute=17 * 60, close="100.00", phase="POST")
    ports = compose_shadow_ports(
        live_read=_LiveRead(),
        live_account_id="9LIVE0001",
        artifacts_root=tmp_path,
        clock=lambda: decision.end_ms,
    )
    # The runtime proves the extended phase at the *repository's* clock, so
    # pinning it to the decision bar's close is what keeps this verdict
    # independent of the hour the suite happens to run at.
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: decision.end_ms
    )
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=ports.read,
        trade=ports.trade,
        authority_kind="shadow",
        account_mode="live",
        stream_health=stream_health,
        # The extended allowances are deployment configuration the shadow
        # read port cannot supply here (ALPACA_LIVE_XH_* is unset in tests),
        # so the sqlite case's own policy is reused verbatim: the two paths
        # must be compared on the liveness gate, nothing else.
        program_leg_policy=_EXTENDED_POLICY,
    )
    binding = _binding(use_rth=False).model_copy(update={"sealed_account_id": ACCOUNT_ID})
    await facade.register_strategy_run(binding)
    try:
        receipt = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-liveness",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=False,
            retained_source_bar=decision,
        )
    finally:
        repo.close()
        evidence.close()
    return receipt.state, receipt.explanation


async def test_a_closed_clock_refuses_a_shadow_extended_enter_with_no_live_feed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shadow world reproduces the live Clerk's own liveness refusal (ADR 0059 D2).

    Mirrors ``test_runtime_program_leg.py``'s sqlite case: the declared window
    resolves POST, so the schedule alone would admit this ENTER, and with no
    stream-health gate installed nothing proves the venue is printing. A
    refusal the real Clerk would make must appear in the paper twin too, or
    the two stop reconciling trade-for-trade.
    """
    state, explanation = await _extended_enter_on_a_closed_clock(
        tmp_path, monkeypatch, stream_health=None
    )

    assert state is EffectOperationState.REJECTED
    assert explanation.startswith("MARKET_LIVENESS_BLOCKED:")


async def test_a_closed_clock_admits_a_shadow_extended_enter_when_the_feed_is_printing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same ENTER, with live evidence the venue is printing, is admitted."""
    state, explanation = await _extended_enter_on_a_closed_clock(
        tmp_path, monkeypatch, stream_health=_stream_health(market_data_healthy=True)
    )

    assert state is not EffectOperationState.REJECTED, explanation
