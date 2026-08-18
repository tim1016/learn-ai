"""S4 (#1262): dual-health submission gate (STREAM_HEALTH_HOLD).

Acceptance criteria pinned here:
- AC1: (feed, exec) health matrix — submit allowed only at (healthy, healthy);
  refusals name the broken stream.
- AC2: a reduction (verified flatten) and a cancel pass while the hold is active.
- AC3: the hold survives restart (journal-derived) and reports which stream
  broke and since when.
- AC4: recovery of both channels does NOT clear the hold; the existing
  clear-hold path does, idempotently.
- AC5: clerk status exposes both channel healths, each with observed_at_ms.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.models import (
    STREAM_HEALTH_HOLD_CODE,
    ChannelHealth,
    ClerkEntryKind,
)
from app.broker.alpaca.clerk.stream_health import (
    ExecutionEvidenceHealth,
    StreamHealthGate,
    execution_channel_health,
    market_data_channel_health,
    stream_health_refusal,
)
from app.broker.contract.errors import BrokerSubmissionHeld
from app.broker.contract.models import BrokerOrderLeg
from app.marketdata.feed import FeedHealth
from tests.broker.alpaca.clerk.test_instance_orders import (
    _FakeBroker,
    _submit_and_fill,
)

_SID = "alpaca-bot-a"
_T0 = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _clerk_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    journal_module.reset_clerk_settings_for_testing()
    yield tmp_path
    journal_module.reset_clerk_settings_for_testing()


def _health(stream: str, healthy: bool, *, observed_at_ms: int = _T0) -> ChannelHealth:
    return ChannelHealth(
        stream=stream,
        healthy=healthy,
        reason="" if healthy else f"{stream} broke for the test",
        observed_at_ms=observed_at_ms,
    )


def _gate(*, feed_healthy: bool, exec_healthy: bool) -> StreamHealthGate:
    return StreamHealthGate(
        market_data=lambda: _health("market_data", feed_healthy),
        execution=lambda: _health("execution", exec_healthy),
    )


def _leg() -> BrokerOrderLeg:
    return BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)


# ── AC1: the health matrix ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("feed_healthy", "exec_healthy"),
    [(True, True), (True, False), (False, True), (False, False)],
)
async def test_submit_allowed_only_when_both_channels_healthy(
    feed_healthy: bool, exec_healthy: bool
) -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(
        read=broker,
        trade=broker,
        stream_health=_gate(feed_healthy=feed_healthy, exec_healthy=exec_healthy),
    )

    if feed_healthy and exec_healthy:
        result = await clerk.submit_for_instance(strategy_instance_id=_SID, legs=[_leg()])
        assert result.results[0].status == "acked"
        return

    with pytest.raises(BrokerSubmissionHeld) as excinfo:
        await clerk.submit_for_instance(strategy_instance_id=_SID, legs=[_leg()])

    assert excinfo.value.reason_code == STREAM_HEALTH_HOLD_CODE
    named = str(excinfo.value.message) + (excinfo.value.detail or "")
    if not feed_healthy:
        assert "market_data" in named
    if not exec_healthy:
        assert "execution" in named
    # Capture-first means a refused submit journals NO intent — but it does
    # journal the durable hold receipt.
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    kinds = [entry.kind for entry in entries]
    assert ClerkEntryKind.INTENT_RECORDED not in kinds
    assert ClerkEntryKind.HOLD_SET in kinds
    assert broker.submit_calls == []


# ── AC2: reductions and cancels pass while the hold is active ──────────


async def test_reduction_and_cancel_pass_while_stream_health_hold_active() -> None:
    broker = _FakeBroker()
    healthy_clerk = AlpacaClerk(read=broker, trade=broker)
    ref = await _submit_and_fill(healthy_clerk, _SID, symbol="SPY", quantity=2.0)

    # Now the streams break: gate refuses a NEW submit and journals the hold.
    held_clerk = AlpacaClerk(
        read=broker, trade=broker, stream_health=_gate(feed_healthy=False, exec_healthy=True)
    )
    with pytest.raises(BrokerSubmissionHeld):
        await held_clerk.submit_for_instance(strategy_instance_id=_SID, legs=[_leg()])

    # A verified flatten (reduction) still passes.
    flatten = await held_clerk.flatten_instance(
        strategy_instance_id=_SID, symbol="SPY", quantity=2.0
    )
    assert flatten.results[0].status == "acked"

    # A cancel still passes (uses the acked order's broker id).
    entries = held_clerk._journal.read_entries()  # type: ignore[union-attr]
    acked = next(
        e for e in entries if e.kind is ClerkEntryKind.SUBMIT_ACKED and e.order_ref == ref
    )
    assert acked.order is not None
    cancel = await held_clerk.cancel(acked.order.order_id)
    assert cancel.status == "acked"


# ── AC3: restart durability + which stream / since when ────────────────


async def test_hold_survives_restart_and_names_the_broken_stream() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(
        read=broker, trade=broker, stream_health=_gate(feed_healthy=False, exec_healthy=True)
    )
    with pytest.raises(BrokerSubmissionHeld):
        await clerk.submit_for_instance(strategy_instance_id=_SID, legs=[_leg()])

    # "Restart": a fresh clerk over the same journal with BOTH channels healthy.
    restarted = AlpacaClerk(
        read=broker, trade=broker, stream_health=_gate(feed_healthy=True, exec_healthy=True)
    )
    status = await restarted.status()
    assert status.hold.active is True
    assert status.hold.reason_code == STREAM_HEALTH_HOLD_CODE
    assert status.hold.reason is not None
    assert "market_data" in status.hold.reason
    assert "observed_at_ms" in status.hold.reason  # since-when travels in the receipt
    assert status.hold.since_ms is not None

    # AC4 first half: recovery alone does NOT clear — submit still refused,
    # now by the journal-derived hold itself.
    with pytest.raises(BrokerSubmissionHeld) as excinfo:
        await restarted.submit_for_instance(strategy_instance_id=_SID, legs=[_leg()])
    assert excinfo.value.reason_code == STREAM_HEALTH_HOLD_CODE


# ── AC4: only the clear-hold exit clears, idempotently ─────────────────


async def test_clear_hold_clears_idempotently_and_submits_resume() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(
        read=broker, trade=broker, stream_health=_gate(feed_healthy=False, exec_healthy=False)
    )
    with pytest.raises(BrokerSubmissionHeld):
        await clerk.submit_for_instance(strategy_instance_id=_SID, legs=[_leg()])

    # Channels recover; operator clears via the existing exit.
    recovered = AlpacaClerk(
        read=broker, trade=broker, stream_health=_gate(feed_healthy=True, exec_healthy=True)
    )
    cleared = await recovered.clear_hold(operator="inkant", reason="streams recovered")
    assert cleared.hold.active is False

    # Idempotent: a second clear is a journal-free NO-OP.
    entries_before = len(recovered._journal.read_entries())  # type: ignore[union-attr]
    again = await recovered.clear_hold(operator="inkant", reason="double click")
    assert again.hold.active is False
    assert len(recovered._journal.read_entries()) == entries_before  # type: ignore[union-attr]

    result = await recovered.submit_for_instance(strategy_instance_id=_SID, legs=[_leg()])
    assert result.results[0].status == "acked"


# ── AC5: status exposes both channel healths with observation times ────


async def test_status_reports_both_channel_healths_with_observed_at_ms() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(
        read=broker, trade=broker, stream_health=_gate(feed_healthy=True, exec_healthy=False)
    )

    status = await clerk.status()

    assert status.channel_healths is not None
    by_stream = {health.stream: health for health in status.channel_healths}
    assert set(by_stream) == {"market_data", "execution"}
    assert by_stream["market_data"].healthy is True
    assert by_stream["execution"].healthy is False
    for health in status.channel_healths:
        assert isinstance(health.observed_at_ms, int)


async def test_status_without_gate_reports_channel_healths_as_none() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    status = await clerk.status()

    assert status.channel_healths is None  # gate not installed ≠ healthy


def test_channel_health_requires_observation_time() -> None:
    with pytest.raises(Exception):
        ChannelHealth(stream="market_data", healthy=True)  # type: ignore[call-arg]


# ── composition folds ──────────────────────────────────────────────────


def _feed_health(*, connected: bool, stale: bool, reason: str = "") -> FeedHealth:
    return FeedHealth(
        connected=connected,
        stale=stale,
        last_bar_ms=_T0,
        reason=reason,
        active_subscription_count=0,
        observed_at_ms=_T0 + 5_000,
    )


@pytest.mark.parametrize(
    ("feed", "expect_healthy", "expect_reason_part"),
    [
        (None, False, "not installed"),
        (_feed_health(connected=True, stale=False), True, ""),
        (_feed_health(connected=False, stale=False, reason="IBKR connection lost"), False, "IBKR connection lost"),
        (_feed_health(connected=True, stale=True, reason="No bar in 300s"), False, "No bar"),
    ],
)
def test_market_data_channel_health_fold(
    feed: FeedHealth | None, expect_healthy: bool, expect_reason_part: str
) -> None:
    health = market_data_channel_health(feed, now_ms=_T0)

    assert health.stream == "market_data"
    assert health.healthy is expect_healthy
    assert expect_reason_part in health.reason
    assert isinstance(health.observed_at_ms, int)


@pytest.mark.parametrize(
    ("connected", "expect_healthy", "expect_reason_part"),
    [
        (None, False, "not running"),
        (True, True, ""),
        (False, False, "disconnected"),
    ],
)
def test_execution_channel_health_fold(
    connected: bool | None, expect_healthy: bool, expect_reason_part: str
) -> None:
    health = execution_channel_health(
        connected=connected,
        connection_changed_at_ms=_T0,
        evidence_health=ExecutionEvidenceHealth(healthy=True, observed_at_ms=_T0),
        now_ms=_T0 + 1,
    )

    assert health.stream == "execution"
    assert health.healthy is expect_healthy
    assert expect_reason_part in health.reason


@pytest.mark.parametrize(
    "evidence_health",
    [ExecutionEvidenceHealth(healthy=False, observed_at_ms=_T0 + 1), None],
)
def test_execution_channel_health_fails_closed_on_unusable_evidence(
    evidence_health: ExecutionEvidenceHealth | None,
) -> None:
    health = execution_channel_health(
        connected=True,
        connection_changed_at_ms=_T0,
        evidence_health=evidence_health,
        now_ms=_T0 + 2,
    )

    assert health.healthy is False
    assert "unusable evidence frame" in health.reason
    assert health.observed_at_ms == (
        evidence_health.observed_at_ms if evidence_health is not None else _T0 + 2
    )


def test_stream_health_refusal_names_every_broken_stream() -> None:
    refusal = stream_health_refusal(_gate(feed_healthy=False, exec_healthy=False))

    assert refusal is not None
    reason, detail = refusal
    assert "market_data" in reason and "execution" in reason
    assert "observed_at_ms" in detail
    assert stream_health_refusal(_gate(feed_healthy=True, exec_healthy=True)) is None
    assert stream_health_refusal(None) is None


def test_stream_health_refusal_uses_symbol_scoped_market_data() -> None:
    gate = StreamHealthGate(
        market_data=lambda: _health("market_data", False),
        execution=lambda: _health("execution", True),
        market_data_for_symbol=lambda symbol: _health(
            "market_data",
            symbol == "SPY",
        ),
    )

    assert stream_health_refusal(gate, symbol="SPY") is None
    refusal = stream_health_refusal(gate, symbol="AAPL")
    assert refusal is not None
    assert "market_data" in refusal[0]
