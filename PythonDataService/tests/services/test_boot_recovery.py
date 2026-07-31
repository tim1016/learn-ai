"""S5 (#1263): container-restart recovery — boot sweep + the named drill.

Acceptance criteria pinned here:
- AC1: after a simulated hard stop with a bot on duty, the boot sweep records
  durable interrupted evidence; the bot is not auto-restarted and never
  renders healthy-on-duty.
- AC2: an intent left unresolved at kill time is resolved by identity on boot
  (present → adopted, provably absent → not-accepted, otherwise → uncertain)
  with no duplicate submission in any branch.
- AC3: a re-start of the interrupted bot hydrates exactly its journal-owned
  exposure and resumes cleanly.
- AC4: starts are refused while the sweep has not run or while recovery left
  an uncertain outcome (fail closed).
- The named drill (kill with an open paper position → restart → prove no
  duplicates, exposure intact, honesty) runs as the integration-shaped test
  at the bottom; the runbook lives in
  docs/superpowers/specs/2026-07-28-alpaca-botctl-restart-drill-runbook.md.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import derive
from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.exposure import project_instance_exposure
from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.clerk.recovery import UNCERTAIN_SUBMIT_GRACE_MS
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrderLeg
from app.engine.live.desired_state import DesiredState
from app.engine.live.order_identity import build_bot_order_namespace
from app.services.bot_runner import (
    BootRecoveryIncompleteError,
    BotTaskRegistry,
    RecoveryUncertainError,
)
from tests.broker.alpaca.clerk.test_instance_orders import (
    _accepted_order,
    _FakeBroker,
    _submit_and_fill,
)
from tests.services.test_bot_runner import _bar, _FakeFeed, _lifecycle_json, _wait_for

_SID = "alpaca-drill-bot"
_T0 = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _clerk_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path / "clerk"))
    journal_module.reset_clerk_settings_for_testing()
    yield tmp_path
    journal_module.reset_clerk_settings_for_testing()


def _artifacts_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


def _registry(tmp_path: Path, feed: _FakeFeed | None) -> BotTaskRegistry:
    return BotTaskRegistry(_artifacts_root(tmp_path), feed_resolver=lambda: feed)


async def _unresolved_probe(clerk: AlpacaClerk) -> int:
    return len(derive.unresolved_intents(await clerk.read_journal_entries()))


# ── AC4: fail-closed start gate ────────────────────────────────────────


async def test_starts_refused_until_boot_sweep_completes(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(BootRecoveryIncompleteError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await registry.run_boot_recovery()
    view = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    assert view.running is True
    await registry.stop("alpaca", _SID)


async def test_starts_refused_while_recovery_left_uncertain_outcome(tmp_path: Path) -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    # Seed an unresolved intent: journal INTENT_RECORDED with no terminal line.
    from app.broker.alpaca.clerk.leg_identity import LegIdentity

    _account_id, journal = await clerk._ensure_journal()
    identity = LegIdentity(
        account_id="PA-TEST",
        operator=_SID,
        intent_id="AAAAAAAAAAAAAAAAAAAAAA",
        order_ref=f"learn-ai/{_SID}/v1:AAAAAAAAAAAAAAAAAAAAAA",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        clock=lambda: _T0,
    )
    await journal.append_async(identity.entry(ClerkEntryKind.INTENT_RECORDED))

    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    # Recovery step is a no-op here (simulating an unreachable broker leaving
    # the intent uncertain); the probe must keep starts refused.
    await registry.run_boot_recovery(
        unresolved_intents_probe=lambda: _unresolved_probe(clerk)
    )

    with pytest.raises(RecoveryUncertainError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")


# ── AC1: interrupted evidence, no auto-restart, never healthy ──────────


async def test_boot_sweep_records_interrupted_evidence_and_never_restarts(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.run_boot_recovery()
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    assert _lifecycle_json(_artifacts_root(tmp_path), _SID)["phase"] == "ON_DUTY"

    # Simulated hard stop: the process dies — tasks vanish, files survive.
    # A hard kill never runs the supervisor's finalizer, so suppress it
    # (finalized=True makes it a no-op) before tearing the task down.
    registry._bots[_SID].finalized = True
    registry._bots[_SID].task.cancel()
    await asyncio.sleep(0)  # evidence stays stale ON_DUTY, as after a kill
    rebooted = _registry(tmp_path, feed)
    report = await rebooted.run_boot_recovery()

    assert report.interrupted_instances == (_SID,)
    lifecycle = _lifecycle_json(_artifacts_root(tmp_path), _SID)
    assert lifecycle["phase"] == "OFF_DUTY"
    assert lifecycle["duty_outcome"]["kind"] == "EXITED_UNVERIFIED"
    assert lifecycle["duty_outcome"]["reason_code"] == "INTERRUPTED_BY_RESTART"

    view = rebooted.status("alpaca", _SID)
    assert view.running is False  # never rendered healthy, never auto-restarted
    assert view.phase == "OFF_DUTY"
    assert view.desired_state == "STOPPED"


async def test_boot_sweep_repairs_interrupted_bot_stranded_as_running(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.run_boot_recovery()
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    registry._bots[_SID].finalized = True
    registry._bots[_SID].task.cancel()
    await asyncio.sleep(0)

    first_reboot = _registry(tmp_path, feed)
    await first_reboot.run_boot_recovery()
    first_reboot._desired_repo(_SID).set(
        DesiredState.RUNNING,
        updated_by="legacy_boot_sweep",
        now_ms=_T0,
        reason="legacy_interrupted_state",
    )
    assert first_reboot.status("alpaca", _SID).desired_state == "RUNNING"

    second_reboot = _registry(tmp_path, feed)
    report = await second_reboot.run_boot_recovery()

    assert report.interrupted_instances == ()
    view = second_reboot.status("alpaca", _SID)
    assert view.phase == "OFF_DUTY"
    assert view.desired_state == "STOPPED"
    assert view.duty_outcome is not None
    assert view.duty_outcome.reason_code == "INTERRUPTED_BY_RESTART"


async def test_boot_sweep_skips_bots_bound_to_unsupported_broker(
    tmp_path: Path,
) -> None:
    """Registry with supported_broker_ids={"alpaca"} must not touch IBKR bots."""
    feed = _FakeFeed([], mode="hold")
    # Deploy and kill an "alpaca" bot to seed ON_DUTY lifecycle state.
    registry = BotTaskRegistry(
        _artifacts_root(tmp_path),
        feed_resolver=lambda: feed,
        supported_broker_ids=frozenset({"alpaca"}),
    )
    await registry.run_boot_recovery()
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    assert _lifecycle_json(_artifacts_root(tmp_path), _SID)["phase"] == "ON_DUTY"
    registry._bots[_SID].finalized = True
    registry._bots[_SID].task.cancel()
    await asyncio.sleep(0)

    # Manually patch the binding file to claim broker="ibkr" — simulating an
    # IBKR-daemon-owned bot whose lifecycle state lives in the same artifacts_root.
    binding_path = (
        _artifacts_root(tmp_path) / "live_state" / _SID / "broker_binding.json"
    )
    import json

    raw = json.loads(binding_path.read_text())
    raw["broker"] = "ibkr"
    binding_path.write_text(json.dumps(raw))

    rebooted = BotTaskRegistry(
        _artifacts_root(tmp_path),
        feed_resolver=lambda: feed,
        supported_broker_ids=frozenset({"alpaca"}),
    )
    report = await rebooted.run_boot_recovery()

    # Sweep must skip the ibkr-tagged bot: no interrupted evidence, lifecycle
    # left untouched as ON_DUTY.
    assert report.interrupted_instances == ()
    assert _lifecycle_json(_artifacts_root(tmp_path), _SID)["phase"] == "ON_DUTY"


# ── AC2: identity resolution branches, no duplicate submission ─────────


async def _seed_unresolved_intent(clerk: AlpacaClerk, *, uncertain: bool) -> str:
    """Drive a real submit whose outcome is lost (BrokerUnavailable)."""
    clerk._trade._error = BrokerUnavailable(  # type: ignore[attr-defined]
        "response lost", broker="alpaca", detail="timeout"
    )
    clerk._trade._lookup_error = BrokerUnavailable(  # type: ignore[attr-defined]
        "lookup down", broker="alpaca", detail="timeout"
    )
    result = await clerk.submit_for_instance(
        strategy_instance_id=_SID,
        legs=[BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)],
    )
    assert result.results[0].status == "uncertain"
    clerk._trade._error = None  # type: ignore[attr-defined]
    clerk._trade._lookup_error = None  # type: ignore[attr-defined]
    return result.results[0].order_ref


class _RecoveryBroker(_FakeBroker):
    """S5 fake: submit/lookup failures + configurable lookup outcome."""

    def __init__(self) -> None:
        super().__init__()
        self._error: Exception | None = None
        self._lookup_error: Exception | None = None
        self.lookup_absent = False

    async def submit(self, leg, *, client_order_id):  # type: ignore[override]
        if self._error is not None:
            raise self._error
        return await super().submit(leg, client_order_id=client_order_id)

    async def get_order_by_client_order_id(self, client_order_id: str):  # type: ignore[override]
        if self._lookup_error is not None:
            raise self._lookup_error
        if self.lookup_absent:
            return None
        return _accepted_order(client_order_id)


@pytest.mark.parametrize("branch", ["present", "absent", "uncertain"])
async def test_unresolved_intent_resolves_by_identity_without_duplicates(
    tmp_path: Path, branch: str
) -> None:
    broker = _RecoveryBroker()
    clerk = AlpacaClerk(read=broker, trade=broker, clock=lambda: _T0)
    await _seed_unresolved_intent(clerk, uncertain=True)
    submits_before = len(broker.submit_calls)

    if branch == "absent":
        broker.lookup_absent = True
        # Past the grace window a provable 404 becomes a terminal not-accepted.
        clerk._clock = lambda: _T0 + UNCERTAIN_SUBMIT_GRACE_MS + 60_000
    elif branch == "uncertain":
        broker._lookup_error = BrokerUnavailable("still down", broker="alpaca", detail="timeout")

    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    await registry.run_boot_recovery(
        recover=clerk.recover,
        unresolved_intents_probe=lambda: _unresolved_probe(clerk),
    )

    # No duplicate submission in ANY branch — resolution is lookup-only.
    assert len(broker.submit_calls) == submits_before

    entries = await clerk.read_journal_entries()
    kinds = [entry.kind for entry in entries]
    unresolved = len(derive.unresolved_intents(entries))
    if branch == "present":
        assert ClerkEntryKind.SUBMIT_ACKED in kinds  # adopted
        assert unresolved == 0
    elif branch == "absent":
        assert ClerkEntryKind.SUBMIT_FAILED in kinds  # closed as not-accepted
        assert unresolved == 0
    else:
        assert unresolved == 1  # held as uncertain — and starts stay refused
        with pytest.raises(RecoveryUncertainError):
            await registry.deploy(
                broker="alpaca", strategy_instance_id=_SID, symbol="SPY"
            )


# ── AC3 + the named drill ──────────────────────────────────────────────


async def test_named_restart_drill_end_to_end(tmp_path: Path) -> None:
    """Kill the container with a bot on duty and an open paper position →
    restart → no duplicate orders, exposure intact and attributed, unresolved
    intents terminally classified, operator surface honest."""
    broker = _RecoveryBroker()
    clerk = AlpacaClerk(read=broker, trade=broker, clock=lambda: _T0)
    feed = _FakeFeed([_bar(_T0)], mode="hold")

    # ── before the kill: bot on duty with an open, attributed position.
    registry = _registry(tmp_path, feed)
    await registry.run_boot_recovery(
        recover=clerk.recover, unresolved_intents_probe=lambda: _unresolved_probe(clerk)
    )
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await _submit_and_fill(clerk, _SID, symbol="SPY", quantity=1.0)
    # One more submit whose response is lost right as the container dies.
    await _seed_unresolved_intent(clerk, uncertain=True)
    submits_at_kill = len(broker.submit_calls)

    # ── the kill: tasks vanish; durable files survive verbatim. Suppress
    # the supervisor finalizer — a hard kill never runs it.
    registry._bots[_SID].finalized = True
    registry._bots[_SID].task.cancel()
    await asyncio.sleep(0)

    # ── restart: fresh registry + fresh clerk over the same durable state.
    clerk2 = AlpacaClerk(read=broker, trade=broker, clock=lambda: _T0 + 1_000)
    rebooted = _registry(tmp_path, feed)
    report = await rebooted.run_boot_recovery(
        recover=clerk2.recover,
        reconcile=None,
        unresolved_intents_probe=lambda: _unresolved_probe(clerk2),
    )

    # Interrupted evidence; honest operator surface.
    assert report.interrupted_instances == (_SID,)
    view = rebooted.status("alpaca", _SID)
    assert view.running is False
    assert view.duty_outcome is not None
    assert view.duty_outcome.reason_code == "INTERRUPTED_BY_RESTART"

    # No duplicate orders: recovery resolved the lost submit by identity only.
    assert len(broker.submit_calls) == submits_at_kill
    entries = await clerk2.read_journal_entries()
    assert len(derive.unresolved_intents(entries)) == 0  # terminally classified

    # Exposure intact and correctly attributed: the pre-kill fill (1 SPY) plus
    # the adopted lost submit (1 SPY, acked but unfilled → no exposure yet).
    exposure = project_instance_exposure(
        entries, namespace=build_bot_order_namespace(_SID)
    )
    assert [(e.symbol, e.quantity) for e in exposure] == [("SPY", 1.0)]

    # AC3: re-start hydrates exactly the journal-owned exposure and resumes.
    restarted_view = await rebooted.deploy(
        broker="alpaca", strategy_instance_id=_SID, symbol="SPY"
    )
    assert restarted_view.running is True
    await _wait_for(lambda: feed.bars_consumed >= 1)
    await rebooted.stop("alpaca", _SID)
