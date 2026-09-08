"""The Clerk runs on the shadow authority: every synthesized fill is bound to the decision bar (ADR 0059 D2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.services.source_bar_ledger import SourceBarLedger
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, _FakeTradePort
from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import _binding
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
