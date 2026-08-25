"""Instance-scoped source-bar evidence for the Real Paper authority (Direction 2)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from app.broker.alpaca.clerk.account_authority import (
    PAPER_EVIDENCE_ACCOUNT_PREFIX,
    paper_evidence_account_id_for_strategy,
    synthetic_account_id_for_strategy,
)
from app.services.bot_binding_authority import RealPaperBindingAuthority
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_lifecycle_projection import AlpacaLifecycleProjector


def _trade_binding(sid: str) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=sid,
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-1",
        created_at_ms=0,
    )


def test_paper_evidence_account_id_for_strategy_is_instance_scoped() -> None:
    account_id = paper_evidence_account_id_for_strategy("bot-a")

    assert account_id == f"{PAPER_EVIDENCE_ACCOUNT_PREFIX}bot-a"
    assert account_id != synthetic_account_id_for_strategy("bot-a")


def test_real_paper_authority_source_bars_opens_instance_scoped_ledger(tmp_path: Path) -> None:
    authority = RealPaperBindingAuthority(
        binding=_trade_binding("bot-a"),
        projector=cast(AlpacaLifecycleProjector, object()),
        external_start_guard=None,
        artifacts_root=tmp_path,
    )

    ledger = authority.source_bars()
    try:
        assert ledger is not None
        assert ledger.account_id == paper_evidence_account_id_for_strategy("bot-a")
        assert ledger.path == (
            tmp_path / "accounts" / "alpaca" / "paper:bot-a" / "source_bars.sqlite3"
        )
    finally:
        ledger.close()
