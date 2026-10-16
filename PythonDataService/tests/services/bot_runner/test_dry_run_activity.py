"""Dry Run activity journal and projection tests.

Split from ``tests/services/test_bot_runner.py`` (issue #1737).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.lean_sidecar.closing_bar import CLOSING_BAR_REASON_CODE
from app.marketdata.feed import ContinuityPolicy, MarketDataBar
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services.bot_binding_repository import (
    BrokerBotBinding,
    alpaca_v1_action_plan,
)
from app.services.bot_dry_run import DryRunActivity, DryRunActivityJournal
from app.services.bot_registry_projection import read_dry_run_activity
from tests._helpers.bot_runner.custody import _SID, _T0, _registry
from tests._helpers.bot_runner.doubles import _FakeClerk, _FakeFeed
from tests._helpers.bot_runner.ema_parity import (
    _ema_parity_bars_through_first_exit,
)
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

from ._support import _install_fake_clerk, _wait_for

pytestmark = pytest.mark.usefixtures("legacy_run_registration")

# The Monday of a trading week whose regulatory fee rates are all pinned.
_FEE_PINNED_MONDAY = date(2026, 9, 21)


def _in_a_fee_pinned_week(bars: list[MarketDataBar]) -> list[MarketDataBar]:
    """Move a February LEAN stream, prices and session-relative times intact, into a fee-pinned week.

    February predates the pinned CAT fee rate, so a funded Dry Run could not
    price a fill there. The stream starts on a Monday, as the target week does.
    """
    from app.lean_sidecar.trading_calendar import session_open_ms_utc
    from app.utils.session_anchors import et_date_at_ms

    shift = session_open_ms_utc(_FEE_PINNED_MONDAY) - session_open_ms_utc(et_date_at_ms(bars[0].end_ms))
    return [bar.model_copy(update={"start_ms": bar.start_ms + shift, "end_ms": bar.end_ms + shift,
        "fetched_at_ms": bar.fetched_at_ms + shift}) for bar in bars]


@pytest.fixture
def _isolated_synthetic_authority() -> None:
    """Keep process-scoped synthetic Clerk state out of neighbouring tests."""
    from app.broker.alpaca.clerk.active_authority import reset_alpaca_clerk_for_testing

    reset_alpaca_clerk_for_testing()
    yield
    reset_alpaca_clerk_for_testing()


def test_dry_run_activity_projection_excludes_prior_run_rows(tmp_path: Path) -> None:
    journal = DryRunActivityJournal(tmp_path)
    for run_id, seq in (("run-prior", 1), ("run-current", 2)):
        journal.append(
            DryRunActivity(
                seq=seq,
                strategy_instance_id=_SID,
                run_id=run_id,
                authority_account_id=f"sim:{_SID}",
                authority_kind="synthetic",
                recorded_at_ms=seq * 1_000,
                bar_ref=f"SPY@{seq * 1_000}",
                intent="ENTER",
                order_ref=f"simulated:{run_id}",
                symbol="SPY",
                side="buy",
                quantity=1,
                fill_price=400,
            )
        )
    binding = BrokerBotBinding(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="dry_run",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-current",
        created_at_ms=_T0,
    )

    activity = read_dry_run_activity(binding, tmp_path, limit=8)

    assert [row.run_id for row in activity] == ["run-current"]


@pytest.mark.asyncio
async def test_dry_run_records_simulated_round_trip_with_zero_broker_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_synthetic_authority: None,
) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    # Dry-run mechanics test reusing the LEAN-parity bars fixture; the
    # deploy runs stamped corpus_coverage=UNCOVERED (ADR 0054), which is
    # irrelevant to what it proves.
    bars = _in_a_fee_pinned_week(_ema_parity_bars_through_first_exit())
    from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
    from app.broker.alpaca.clerk.sqlite import runtime as clerk_runtime
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
    from app.utils import timestamps

    # This fixture compresses several sessions into milliseconds. Advance the
    # account clock and its cadence with the feed, not the process wall clock.
    current_ms = [bars[0].end_ms]
    monkeypatch.setattr(timestamps, "time", SimpleNamespace(time=lambda: current_ms[0] / 1000))
    monkeypatch.setattr(clerk_runtime, "prepared_top_of_book", lambda _symbol, _now: SimpleNamespace(ask=bars[0].close))

    original_open, original_initialize = ClerkSqliteRepository.open, ClerkSqliteRepository.initialize
    replay_lease_ms = bars[-1].end_ms - bars[0].end_ms + 86_400_000
    def replay_open(**kwargs: Any) -> ClerkSqliteRepository:
        return original_open(**{**kwargs, "lease_ttl_ms": replay_lease_ms})
    def replay_initialize(**kwargs: Any) -> ClerkSqliteRepository:
        return original_initialize(**{**kwargs, "lease_ttl_ms": replay_lease_ms})
    monkeypatch.setattr(ClerkSqliteRepository, "open", replay_open)
    monkeypatch.setattr(ClerkSqliteRepository, "initialize", replay_initialize)

    class ClockedFeed(_FakeFeed):
        async def stream_bars(self, symbol: str, *, use_rth: bool = True, continuity: ContinuityPolicy | None = None) -> AsyncIterator[MarketDataBar]:
            async for bar in super().stream_bars(symbol, use_rth=use_rth, continuity=continuity):
                runtime = get_clerk_runtime(f"sim:{_SID}")
                assert runtime is not None and runtime.sqlite_repository is not None and runtime.envelope_sync is not None
                current_ms[0] = bar.end_ms
                await runtime.envelope_sync.tick()
                yield bar

    feed = ClockedFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    admitted = await registry.deploy_with_admission(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        mode="dry_run",
        quantity=3,
        budget_consent=DeployBudgetConsent(committed_cents=1_000_000, risk_revision=0, actor="owner",
            request_fingerprint="reviewed-dry-run-roundtrip", world="synthetic"),
    )
    deployed = admitted.bot
    await _wait_for(lambda: feed.bars_consumed == len(bars))
    await _wait_for(lambda: len(registry.dry_run_activity("alpaca", _SID)) >= 2)

    activity = registry.dry_run_activity("alpaca", _SID)
    assert deployed.mode == "dry_run"
    assert clerk.calls == []
    assert [(row.intent, row.side, row.quantity, row.simulated) for row in activity[:2]] == [
        ("ENTER", "buy", 3.0, True),
        ("EXIT", "sell", 3.0, True),
    ]
    assert {(row.authority_account_id, row.authority_kind) for row in activity} == {
        (f"sim:{_SID}", "synthetic")
    }
    # The panel suffix is derived from the synthetic Clerk's real custody
    # operations, not a runner-minted simulated order namespace.
    assert all(not row.order_ref.startswith("simulated:") for row in activity)
    from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
    from app.services.source_bar_ledger import SourceBarLedger

    account_id = synthetic_account_id_for_strategy(_SID)
    runtime = get_clerk_runtime(account_id)
    assert runtime is not None
    assert runtime.authority_kind == "synthetic"
    assert runtime.selected_account_id == account_id
    retained = SourceBarLedger(artifacts_root=tmp_path, account_id=account_id)
    assert len(retained.bars(provider="lean-golden", symbol="SPY")) == len(bars)
    assert all(row.bar_ref.startswith(f"source-bar:{account_id}:") for row in activity)
    # The EMA's 15-minute decision is priced at its own close, not at
    # whichever raw minute was latest when the decision surfaced. Each journal
    # receipt must name and price the unique durable bar at the intent's clock.
    for row in activity:
        decision_bar = retained.find_by_closed_end(
            provider="lean-golden",
            symbol="SPY",
            end_ms=row.recorded_at_ms,
        )
        assert decision_bar is not None
        assert (row.bar_ref, row.fill_price) == (decision_bar.bar_ref, float(decision_bar.close))
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_dry_run_refuses_a_decision_taken_after_its_delivery_allowance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_synthetic_authority: None,
) -> None:
    """#2345: the Dry Run runner applies the same staleness gate as the trade runner.

    This package pins the wall clock to each fed bar's close by default; this
    test re-pins it a day past every decision bar.
    """
    import app.services.feed_continuity_policy as feed_continuity_policy
    from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
    from app.broker.alpaca.clerk.active_authority import get_clerk_runtime

    bars = _ema_parity_bars_through_first_exit()
    monkeypatch.setattr(feed_continuity_policy, "now_ms_utc", lambda: bars[-1].end_ms + 86_400_000)
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        mode="dry_run",
        quantity=3,
    )
    await _wait_for(lambda: feed.bars_consumed == len(bars))
    runtime = get_clerk_runtime(synthetic_account_id_for_strategy(_SID))
    assert runtime is not None and runtime.clerk is not None

    def _late_refusals() -> list[str]:
        receipts = runtime.clerk.repository.decision_receipt_tail(strategy_instance_id=_SID, limit=500)
        return [
            receipt.outcome
            for receipt in receipts
            if json.loads(receipt.facts_json)["reason_code"] == "DECISION_LATE"
        ]

    await _wait_for(lambda: len(_late_refusals()) >= 1)

    # The refused ENTER was discarded, so the strategy never held a position
    # and never staged the EXIT; nothing reached the synthetic authority.
    assert _late_refusals() == ["blocked"]
    assert registry.dry_run_activity("alpaca", _SID) == []
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_dry_run_refuses_a_market_enter_decided_on_the_last_bar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_synthetic_authority: None,
) -> None:
    """#2607: the sandbox refuses a closing-bar ENTER exactly as Live does.

    QQQ's first EMA ENTER is decided on its second session's last bucket --
    the closing bar, decided only after the close. The Dry Run runner shares
    the live runner's closing-bar screen, so the ENTER is discarded and
    receipted ``CLOSING_BAR`` before its synthetic Clerk is reached, and no
    simulated fill is recorded at a close Live never trades.
    """
    from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
    from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
    from app.broker.alpaca.clerk.sqlite import runtime as clerk_runtime
    from app.lean_sidecar.trading_calendar import session_close_ms_utc
    from app.utils import timestamps
    from app.utils.session_anchors import et_date_at_ms
    from tests._helpers.bot_runner.ema_parity import ema_bars_through_a_last_bar_enter

    bars = _in_a_fee_pinned_week(ema_bars_through_a_last_bar_enter())
    close_ms = session_close_ms_utc(et_date_at_ms(bars[-1].end_ms))
    # The synthetic Clerk's clock reads the instant the last bar's decision
    # lands, 0.6 s after the close; every earlier bucket decides no action.
    monkeypatch.setattr(timestamps, "time", SimpleNamespace(time=lambda: (close_ms + 600) / 1000))
    # A funded sandbox prices the entry off a quote, as the round trip above does.
    monkeypatch.setattr(clerk_runtime, "prepared_top_of_book", lambda _symbol, _now: SimpleNamespace(ask=bars[-1].close))
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy_with_admission(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="QQQ",
        mode="dry_run",
        quantity=3,
        budget_consent=DeployBudgetConsent(committed_cents=1_000_000, risk_revision=0, actor="owner",
            request_fingerprint="reviewed-dry-run-last-bar", world="synthetic"),
    )
    await _wait_for(lambda: feed.bars_consumed == len(bars))
    runtime = get_clerk_runtime(synthetic_account_id_for_strategy(_SID))
    assert runtime is not None and runtime.clerk is not None

    def _refusals() -> list[dict[str, Any]]:
        receipts = runtime.clerk.repository.decision_receipt_tail(strategy_instance_id=_SID, limit=500)
        return [
            {"outcome": receipt.outcome, **json.loads(receipt.facts_json)}
            for receipt in receipts
            if receipt.outcome == "blocked"
        ]

    await _wait_for(lambda: bool(_refusals() or registry.dry_run_activity("alpaca", _SID)))

    assert registry.dry_run_activity("alpaca", _SID) == []
    (refusal,) = _refusals()
    assert (refusal["reason_code"], refusal["decision_bar_close_ms"]) == (CLOSING_BAR_REASON_CODE, close_ms)
    assert clerk.calls == []
    await registry.stop("alpaca", _SID)


def test_dry_run_activity_journal_lifts_legacy_authority_fields(tmp_path: Path) -> None:
    """Pre-authority journal rows remain readable after the schema extension."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    (instance_dir / "dry_run_activity.jsonl").write_text(
        json.dumps(
            {
                "seq": 1,
                "strategy_instance_id": _SID,
                "run_id": "run-legacy",
                "recorded_at_ms": _T0,
                "bar_ref": "SPY@1700000000000",
                "intent": "ENTER",
                "order_ref": "simulated:legacy",
                "symbol": "SPY",
                "side": "buy",
                "quantity": 1.0,
                "fill_price": 400.0,
                "simulated": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rows = DryRunActivityJournal(instance_dir).tail(1)

    assert rows[0].authority_account_id == f"sim:{_SID}"
    assert rows[0].authority_kind == "synthetic"


@pytest.mark.asyncio
async def test_dry_run_deploy_at_an_uncovered_parameter_point_is_admitted_and_stamped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_synthetic_authority: None,
) -> None:
    """ADR 0054 end to end: no bypass helper, no validated-point edit.

    The former relaxed paper experiment is deliberately outside the current
    Live-qualified default point. Dry Run's synthetic authority is a paper
    environment, so the run is admitted, stamped on the decision, and its
    per-run evidence keeps the stamp.
    """
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    started = await registry.deploy_with_admission(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        mode="dry_run",
        quantity=1,
        strategy_params={"gap": 0.0, "gap_bps": 0.0, "rsi_min": 30.0, "rsi_max": 70.0},
        strategy_param_origins={
            "gap": "deploy_override",
            "gap_bps": "deploy_override",
            "rsi_min": "deploy_override",
            "rsi_max": "deploy_override",
        },
    )
    try:
        assert started.admission.allowed is True
        assert "Corpus coverage is UNCOVERED" in started.admission.explanation
        assert "program-corpus-coverage:UNCOVERED" in started.admission.evidence_refs
        run_id = started.bot.active_run_id
        assert run_id is not None
        evidence = json.loads(
            (tmp_path / "live_state" / _SID / "program_build_evidence" / f"{run_id}.json").read_text(
                encoding="utf-8"
            )
        )
        assert evidence["corpus_coverage"] == "UNCOVERED"
        assert evidence["schema_version"] == 3
    finally:
        await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "broker_unconfigured", "account_pin_mismatch", "apply_preflight_refused",
    "profiles_database_unavailable", "retired_environment_settings",
])
async def test_dry_run_start_reports_its_own_unpriceable_authority_and_binding_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    _isolated_synthetic_authority: None, reason: str,
) -> None:
    from app.broker.alpaca import active_binding
    from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime

    refusal = active_binding.UnboundBroker(
        reason=reason,
        message=f"The worker binding failed: {reason}.",
        next_step=f"Repair {reason} before starting a new run.",
    )
    monkeypatch.setattr(active_binding, "_binding", None)
    monkeypatch.setattr(active_binding, "_refusal", refusal)
    assert get_active_clerk_runtime() is None
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    decision = await registry.preview_start_admission(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="dry_run", use_rth=False,
    )
    assert decision.allowed is False
    assert decision.reason_code == reason
    assert refusal.message in decision.explanation
    assert "entry allowance comes from the Alpaca paper settings" in decision.explanation
    assert "could not be loaded" in decision.explanation
    assert refusal.next_step in decision.next_step
    assert "Fix the Alpaca connection" in decision.next_step


def test_a_fill_priced_at_its_own_minute_writes_the_row_it_always_wrote(tmp_path: Path) -> None:
    """The journal refuses a field it does not know, so an absent one is not written as null (#2912).

    Code from before the field existed must still read every row of a bot
    whose fills were priced at their own minute.
    """
    journal = DryRunActivityJournal(tmp_path)
    row = {
        "seq": 1, "strategy_instance_id": _SID, "run_id": "run-1",
        "authority_account_id": f"sim:{_SID}", "authority_kind": "synthetic", "recorded_at_ms": _T0,
        "bar_ref": "bar-1", "intent": "EXIT", "order_ref": "order-1", "symbol": "QQQ", "side": "sell",
        "quantity": 1.0, "fill_price": 40.3,
    }
    journal.append(DryRunActivity(**row))
    journal.append(DryRunActivity(**{**row, "seq": 2, "order_ref": "order-2", "price_bar_end_ms": _T0 - 120_000}))

    own_minute, older = (
        json.loads(line) for line in (tmp_path / "dry_run_activity.jsonl").read_text().splitlines()
    )
    assert "price_bar_end_ms" not in own_minute
    assert older["price_bar_end_ms"] == _T0 - 120_000
    assert [activity.price_bar_end_ms for activity in journal.tail(5)] == [None, _T0 - 120_000]


_A_FILL_IN_SHARES = {
    "seq": 1, "strategy_instance_id": _SID, "run_id": "run-1",
    "authority_account_id": f"sim:{_SID}", "authority_kind": "synthetic", "recorded_at_ms": _T0,
    "bar_ref": "bar-1", "intent": "ENTER", "order_ref": "order-1", "symbol": "SPY", "side": "buy",
    "quantity": 1.0, "fill_price": 585.0,
}
_OF_AN_OPTION_FILL = {
    "symbol": "SPY261009C00585000", "fill_price": 4.12, "underlying": "SPY", "multiplier": 100,
    "fill_model": "natural_price", "quote_age_ms": 250, "contract_reference": "ibkr_listing",
}


def test_a_fill_in_shares_writes_none_of_an_option_fills_keys_and_an_option_fill_reads_back(tmp_path: Path) -> None:
    """A row for shares is what it was before a row could name a contract (#2913): the journal refuses a key
    it does not know, so code from before must still read every fill a stock bot makes."""
    journal = DryRunActivityJournal(tmp_path)
    journal.append(DryRunActivity(**_A_FILL_IN_SHARES))
    journal.append(DryRunActivity(**{**_A_FILL_IN_SHARES, **_OF_AN_OPTION_FILL, "seq": 2, "order_ref": "order-2"}))

    shares, contract = (json.loads(line) for line in (tmp_path / "dry_run_activity.jsonl").read_text().splitlines())
    in_shares, in_a_contract = journal.tail(5)

    assert shares == {**_A_FILL_IN_SHARES, "simulated": True}
    assert {key: contract[key] for key in _OF_AN_OPTION_FILL} == _OF_AN_OPTION_FILL
    assert (in_shares.underlying, in_shares.multiplier, in_shares.fill_model) == (None, None, None)
    assert (in_a_contract.symbol, in_a_contract.underlying, in_a_contract.multiplier) == (
        "SPY261009C00585000", "SPY", 100,
    )
    assert (in_a_contract.fill_model, in_a_contract.quote_age_ms, in_a_contract.contract_reference) == (
        "natural_price", 250, "ibkr_listing",
    )


@pytest.mark.parametrize(
    "half_an_option_fill",
    [
        pytest.param({"multiplier": 100}, id="a multiplier and no underlying"),
        pytest.param({"underlying": "SPY"}, id="an underlying and no multiplier"),
        pytest.param({"fill_model": "natural_price"}, id="a fill model on a fill in shares"),
        pytest.param({"quote_age_ms": 250}, id="a quote's age on a fill in shares"),
        pytest.param({"contract_reference": "ibkr_listing"}, id="a contract reference on a fill in shares"),
    ],
)
def test_a_row_is_a_fill_in_shares_or_a_fill_in_a_contract_never_half_of_each(half_an_option_fill: dict[str, Any]) -> None:
    """A premium with no multiplier beside it would be read as a share price."""
    with pytest.raises(ValueError, match="option"):
        DryRunActivity(**{**_A_FILL_IN_SHARES, **half_an_option_fill})
