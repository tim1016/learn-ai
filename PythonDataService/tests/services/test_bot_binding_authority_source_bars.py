"""Instance-scoped source-bar evidence, and the world the primary authority custodies in (Direction 2)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from app.broker.alpaca.clerk.account_authority import (
    PAPER_EVIDENCE_ACCOUNT_PREFIX,
    paper_evidence_account_id_for_strategy,
    synthetic_account_id_for_strategy,
)
from app.broker.alpaca.clerk.active_authority import (
    ActiveAlpacaClerk,
    ActiveClerkRuntime,
    reset_alpaca_clerk_for_testing,
    set_active_clerk_runtime,
    set_alpaca_clerk,
)
from app.services.bot_binding_authority import (
    PrimaryAccountBindingAuthority,
    primary_custody_kind,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_lifecycle_projection import AlpacaLifecycleProjector
from app.services.bot_start_admission import StartAdmissionUnavailable


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
    authority = PrimaryAccountBindingAuthority(
        binding=_trade_binding("bot-a"),
        projector=cast(AlpacaLifecycleProjector, object()),
        external_start_guard=None,
        artifacts_root=tmp_path,
        custody_kind=lambda: "real_paper",
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


def test_primary_authority_on_the_shadow_world_opens_the_shadow_evidence_ledger(
    tmp_path: Path,
) -> None:
    authority = PrimaryAccountBindingAuthority(
        binding=_trade_binding("bot-a"),
        projector=cast(AlpacaLifecycleProjector, object()),
        external_start_guard=None,
        artifacts_root=tmp_path,
        custody_kind=lambda: "shadow",
    )

    ledger = authority.source_bars()
    try:
        assert ledger.account_id == "shadow-evidence:bot-a"
    finally:
        ledger.close()


def _clerk_double() -> ActiveAlpacaClerk:
    """Any installed Clerk: the custody world is read off the runtime, never the Clerk."""
    return cast(ActiveAlpacaClerk, object())


def test_primary_custody_kind_refuses_to_guess_with_no_authority_installed() -> None:
    """The one function deciding where a live-account bot's evidence lands."""
    reset_alpaca_clerk_for_testing()
    try:
        with pytest.raises(StartAdmissionUnavailable):
            primary_custody_kind()
    finally:
        reset_alpaca_clerk_for_testing()


def test_primary_custody_kind_on_the_real_paper_compat_seam_is_real_paper() -> None:
    """``set_alpaca_clerk`` declares no authority kind; the fallback keeps paper evidence stable."""
    set_alpaca_clerk(_clerk_double())
    try:
        assert primary_custody_kind() == "real_paper"
    finally:
        reset_alpaca_clerk_for_testing()


def test_primary_custody_kind_on_the_shadow_authority_is_shadow() -> None:
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="shadow",
            clerk=_clerk_double(),
            account_id="shadow:9LIVE0001",
            account_authority_kind="shadow",
        )
    )
    try:
        assert primary_custody_kind() == "shadow"
    finally:
        reset_alpaca_clerk_for_testing()


def test_primary_custody_kind_on_a_synthetic_runtime_is_synthetic() -> None:
    """Pinned, not asserted from the caller's needs: no primary boot selects synthetic today."""
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="synthetic",
            clerk=_clerk_double(),
            account_id="sim:bot-a",
        )
    )
    try:
        assert primary_custody_kind() == "synthetic"
    finally:
        reset_alpaca_clerk_for_testing()
