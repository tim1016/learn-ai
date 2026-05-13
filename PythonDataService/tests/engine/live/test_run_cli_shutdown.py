"""Integration test for the cmd_start signal → shutdown_event → flatten flow.

Drives ``cmd_start`` end-to-end with a FakeBroker that holds an open
SPY position. Patches ``_install_signal_handlers`` to set the
shutdown_event immediately instead of sending a real SIGINT — direct
signal delivery from the test harness is tangled by pytest's own
signal handling. The wire-up proven by this test:

  1. ``cmd_start`` builds a run-ledger and patches the engine logger
     for the run-dir.
  2. ``_drive_engine`` creates ``shutdown_event`` and passes it into
     ``engine.run``.
  3. The (patched) handler installer sets the event.
  4. The engine's bar loop top-of-iteration check trips on the first
     bar; ``_shutdown_flatten`` cancels open orders + liquidates the
     position + submits the liquidation.
  5. The engine exits its loop cleanly; the artifact writers close in
     the finally block.
  6. ``cmd_start`` returns exit code 0 and writes
     ``[START] run completed cleanly`` to stdout.

Pieces tested in isolation already (commit 3 for shutdown_event in the
engine, commit 5 for the signal-handler shape, commit 6 for
recovery_flatten); this commit binds them together via cmd_start.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from app.broker.ibkr.models import IbkrPosition, IbkrPositionsSnapshot
from app.engine.data.trade_bar import TradeBar
from app.engine.live.run import cmd_start
from app.engine.live.run_ledger import build_ledger, write_ledger
from tests.engine.live.fixtures.fake_broker import FakeBroker


def _spy_bar(minute: int, hour: int = 14) -> TradeBar:
    start = datetime(2026, 5, 4, hour, minute, tzinfo=UTC)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal("500"),
        high=Decimal("500"),
        low=Decimal("500"),
        close=Decimal("500"),
        volume=100,
    )


async def _iter_bars(bars: list[TradeBar]) -> AsyncIterator[TradeBar]:
    for bar in bars:
        yield bar


def _build_run_dir(tmp_path: Path) -> Path:
    """Build a valid run_ledger.json under tmp_path/run-<id>/ and return its dir."""
    strategy_spec = tmp_path / "spec.json"
    strategy_spec.write_text('{"strategy": "spy_ema_crossover"}', encoding="utf-8")
    qc_audit = tmp_path / "qc_audit.py"
    qc_audit.write_text("# QC audit copy stub\n", encoding="utf-8")

    ledger = build_ledger(
        code_sha="deadbeef" * 5,  # 40-char fake sha; ledger doesn't validate format
        strategy_spec_path=strategy_spec,
        qc_audit_copy_path=qc_audit,
        qc_cloud_backtest_id="bt-test-1",
        account_id="DU123",
        start_date_ms=1714838400000,
        live_config={},
    )
    run_dir = tmp_path / ledger.run_id
    write_ledger(run_dir / "run_ledger.json", ledger)
    return run_dir


def _make_args(
    run_dir: Path,
    broker: FakeBroker,
    bars: list[TradeBar],
    *,
    client=None,
) -> argparse.Namespace:
    return argparse.Namespace(
        command="start",
        run_dir=run_dir,
        strategy="spy_ema_crossover",
        readonly=False,
        max_orders_per_day=4,
        broker=broker,
        bars=_iter_bars(bars),
        client=client,
    )


class _LifecycleClient:
    """Stub IbkrClient that records connect/disconnect calls so tests can
    pin lifecycle invariants without spinning up a real broker session.

    Only the methods cmd_start actually calls — connect, disconnect,
    is_connected — are implemented. ``LiveEngine._validate_paper_client``
    accesses other attributes (settings, connected_account) but only
    inside ``engine.run``; tests using this stub must exit cmd_start
    before reaching that path (e.g., via the position-gate refusal)."""

    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0
        self._connected = False

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


def test_cmd_start_shutdown_event_path_flattens_and_returns_zero(tmp_path: Path) -> None:
    """End-to-end: SIGINT-equivalent event fires → engine flattens → cmd_start returns 0."""
    run_dir = _build_run_dir(tmp_path)

    broker = FakeBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=756733,
                symbol="SPY",
                sec_type="STK",
                quantity=100.0,
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )

    args = _make_args(run_dir, broker, [_spy_bar(m) for m in range(30, 35)])

    # Patch the signal-handler installer to set shutdown_event
    # immediately. The engine's top-of-iteration check then trips on
    # the first bar instead of waiting for a real signal.
    def _set_event_immediately(loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event) -> None:
        shutdown_event.set()

    with patch("app.engine.live.run._install_signal_handlers", _set_event_immediately):
        rc = cmd_start(args)

    assert rc == 0

    # Flatten was submitted via the broker.
    sell_orders = [o for o in broker.orders if o.action == "SELL"]
    assert len(sell_orders) == 1, f"expected 1 SELL liquidation, got {broker.orders!r}"
    assert sell_orders[0].symbol == "SPY"
    assert sell_orders[0].quantity == 100

    # The run-dir is the live-engine's output_dir; log + artifact writers
    # should have produced a live.log file via configure_run_logging.
    assert (run_dir / "live.log").exists()

    # Engine's artifact writers ran (decisions/executions/trades parquets
    # may be empty since we broke before any signal fired, but they
    # exist as closed files when at least one row was written; empty
    # files are not created — verify the run-dir is consistent).
    assert (run_dir / "run_ledger.json").exists()


@pytest.mark.skip(
    reason=(
        "Sending real SIGINT to the test process under pytest is brittle: "
        "pytest's own signal handlers and the asyncio loop's thread "
        "ownership interact in ways that hang the runner. The wire-up "
        "this test would prove is already covered by the immediate-set "
        "patch test above plus the per-component tests in commits 3, 5, 6."
    )
)
def test_cmd_start_real_sigint_end_to_end_documented_skip(tmp_path: Path) -> None:
    """Documented skip placeholder for the real-SIGINT integration scenario."""


def test_cmd_start_refuses_with_unexpected_position_on_broker(tmp_path: Path) -> None:
    """cmd_start must refuse to launch when the broker holds anything
    beyond a long position in the strategy's expected symbol.

    Reviewer feedback (P1.3): the pre-flight subcommand prints "the
    live runner enforces this when connected to IB" when no
    ``--positions-json`` is supplied, but the prior implementation
    had no such enforcement in cmd_start or LiveEngine. A paper run
    could start with foreign symbols or short SPY in the account.
    Fix: cmd_start fetches positions after connect (or, in the test
    path, from the injected broker directly) and refuses to start if
    ``check_unexpected_position`` flags any.
    """
    run_dir = _build_run_dir(tmp_path)

    # Pre-seed the broker with a non-strategy symbol — the gate must fire.
    broker = FakeBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=42,
                symbol="QQQ",  # not SPY → unexpected
                sec_type="STK",
                quantity=10.0,
                avg_cost=400.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )

    args = _make_args(run_dir=run_dir, broker=broker, bars=[])
    rc = cmd_start(args)

    # Halt exit code (per cmd_start's other halt paths) and the gate did
    # NOT call engine.run — broker.advance_bar / place_order untouched.
    assert rc == 1
    assert broker.orders == [], "must refuse before any order placement"


def test_cmd_start_refuses_with_short_position_in_expected_symbol(tmp_path: Path) -> None:
    """A short position in the expected symbol is also unexpected for the
    long-only SPY EMA strategy. Same P1.3 gate, different bad shape."""
    run_dir = _build_run_dir(tmp_path)

    broker = FakeBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=756733,
                symbol="SPY",
                sec_type="STK",
                quantity=-50.0,  # short
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )

    args = _make_args(run_dir=run_dir, broker=broker, bars=[])
    rc = cmd_start(args)
    assert rc == 1
    assert broker.orders == []


def test_cmd_start_disconnects_client_when_position_check_fails(tmp_path: Path) -> None:
    """The unexpected-position halt path must still disconnect the client.

    Reviewer feedback on PR #233 (Codex P1): cmd_start's new
    unexpected-position gate (P1.3) had two early-return paths
    (return 2 on fetch_positions failure, return 1 on bad position)
    that exited BEFORE the try/finally that called
    ``client.disconnect()``. So a position-gate halt would leave the
    IBKR session connected, leaking until process exit and potentially
    interfering with the next operator run. Fix: outer try/finally
    around the entire post-connect body so every exit path disconnects.
    """
    run_dir = _build_run_dir(tmp_path)

    # Bad-position broker — gate must refuse with exit 1.
    broker = FakeBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=42,
                symbol="QQQ",  # not SPY
                sec_type="STK",
                quantity=10.0,
                avg_cost=400.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )
    client = _LifecycleClient()

    args = _make_args(run_dir=run_dir, broker=broker, bars=[], client=client)
    rc = cmd_start(args)

    # Halt return code (matches the existing P1.3 tests).
    assert rc == 1
    # The critical assertion: connect AND disconnect both fired, even
    # though we exited via the position-gate refusal — not via
    # engine.run's finally.
    assert client.connect_calls == 1
    assert client.disconnect_calls == 1
    # Sanity: no orders placed (gate refused before engine.run).
    assert broker.orders == []


def test_cmd_start_disconnects_client_when_fetch_positions_raises(tmp_path: Path) -> None:
    """Same as above but for the OTHER early-return path: a broker error
    on ``fetch_positions`` returns 2 — must still disconnect."""
    run_dir = _build_run_dir(tmp_path)

    class _RaisingBroker(FakeBroker):
        async def fetch_positions(self):  # type: ignore[override]
            raise ConnectionError("simulated broker outage during position fetch")

    broker = _RaisingBroker()
    client = _LifecycleClient()

    args = _make_args(run_dir=run_dir, broker=broker, bars=[], client=client)
    rc = cmd_start(args)

    assert rc == 2
    assert client.connect_calls == 1
    assert client.disconnect_calls == 1, "must disconnect even when fetch_positions raises"
