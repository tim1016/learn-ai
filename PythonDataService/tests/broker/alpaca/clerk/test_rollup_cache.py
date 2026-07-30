"""Tests for the incremental per-bot rollup cache (broker-v2 S0).

Covers:
- Basic fill + decision receipt accumulation
- Exposure calculation
- No O(journal) per-request scan (100-bot performance fixture)
- Thread-safety smoke
- Cold-start bootstrap
"""

from __future__ import annotations

import time

import pytest

from app.broker.alpaca.clerk.decision_journal import DecisionReceipt
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.rollup_cache import BotRollup, BotRollupCache
from app.broker.contract.models import OrderSide

_ACCT = "acct-test"


def _fill(
    *,
    sid: str,
    side: OrderSide = OrderSide.BUY,
    qty: float = 100.0,
    price: float = 500.0,
    ts_ms: int = 1_000_000,
    symbol: str = "SPY",
    event_key: str | None = None,
) -> FillRecord:
    key = event_key or f"exec:{ts_ms}:{sid}"
    return FillRecord(
        account_id=_ACCT,
        sid=sid,
        intent_id=f"int_{ts_ms}",
        order_ref=f"learn-ai/{sid}/v1:int_{ts_ms}",
        event_key=key,
        symbol=symbol,
        side=side,
        quantity=qty,
        fill_price=price,
        filled_at_ms=ts_ms,
        fee=None,
    )


def _receipt(seq: int, outcome: str = "no_action", ts_ms: int = 1_000_000) -> DecisionReceipt:
    return DecisionReceipt(
        seq=seq,
        ts_ms=ts_ms,
        bar_ref=f"SPY@{ts_ms}",
        outcome=outcome,  # type: ignore[arg-type]
        reason_code="TEST",
    )


# ── Basic accumulation ────────────────────────────────────────────────────────


def test_unknown_sid_returns_zero_rollup() -> None:
    cache = BotRollupCache()
    rollup = cache.get_rollup("nonexistent")
    assert rollup.sid == "nonexistent"
    assert rollup.exposure == {}
    assert rollup.fills_today == 0
    assert rollup.realized_pnl_today == 0.0
    assert rollup.needs_attention is False


def test_fill_appended_updates_exposure() -> None:
    cache = BotRollupCache()
    cache.on_fill_appended(_fill(sid="bot1", side=OrderSide.BUY, qty=100.0))
    rollup = cache.get_rollup("bot1")
    assert rollup.exposure.get("SPY", 0.0) == pytest.approx(100.0)


def test_buy_sell_cancel_exposure() -> None:
    """BUY 100 then SELL 100 → flat (exposure dict empty)."""
    cache = BotRollupCache()
    cache.on_fill_appended(_fill(sid="bot1", side=OrderSide.BUY, qty=100.0, event_key="e1"))
    cache.on_fill_appended(_fill(sid="bot1", side=OrderSide.SELL, qty=100.0, event_key="e2"))
    rollup = cache.get_rollup("bot1")
    assert rollup.exposure == {}


def test_partial_exposure_tracked() -> None:
    cache = BotRollupCache()
    cache.on_fill_appended(_fill(sid="bot1", side=OrderSide.BUY, qty=100.0, event_key="e1"))
    cache.on_fill_appended(_fill(sid="bot1", side=OrderSide.SELL, qty=60.0, event_key="e2"))
    rollup = cache.get_rollup("bot1")
    assert rollup.exposure.get("SPY", 0.0) == pytest.approx(40.0)


def test_last_activity_at_ms_updated_by_fill() -> None:
    cache = BotRollupCache()
    cache.on_fill_appended(_fill(sid="bot1", ts_ms=1_234_567, event_key="e1"))
    rollup = cache.get_rollup("bot1")
    assert rollup.last_activity_at_ms == 1_234_567


def test_last_activity_at_ms_updated_by_decision() -> None:
    cache = BotRollupCache()
    cache.on_decision_appended(_receipt(seq=1, ts_ms=9_999_999), sid="bot1")
    rollup = cache.get_rollup("bot1")
    assert rollup.last_activity_at_ms == 9_999_999


def test_needs_attention_when_blocked() -> None:
    cache = BotRollupCache()
    cache.on_decision_appended(_receipt(seq=1, outcome="blocked", ts_ms=1000), sid="bot1")
    rollup = cache.get_rollup("bot1")
    assert rollup.needs_attention is True


def test_needs_attention_false_when_no_action() -> None:
    cache = BotRollupCache()
    cache.on_decision_appended(_receipt(seq=1, outcome="no_action", ts_ms=1000), sid="bot1")
    rollup = cache.get_rollup("bot1")
    assert rollup.needs_attention is False


# ── Bot isolation ─────────────────────────────────────────────────────────────


def test_bots_isolated() -> None:
    cache = BotRollupCache()
    cache.on_fill_appended(_fill(sid="bot-a", qty=100.0, event_key="ea"))
    cache.on_fill_appended(_fill(sid="bot-b", qty=200.0, symbol="QQQ", event_key="eb"))
    a = cache.get_rollup("bot-a")
    b = cache.get_rollup("bot-b")
    assert a.exposure.get("SPY", 0.0) == pytest.approx(100.0)
    assert a.exposure.get("QQQ", 0.0) == pytest.approx(0.0)
    assert b.exposure.get("QQQ", 0.0) == pytest.approx(200.0)
    assert b.exposure.get("SPY", 0.0) == pytest.approx(0.0)


# ── Bootstrap (cold-start) ────────────────────────────────────────────────────


def test_bootstrap_from_fills() -> None:
    cache = BotRollupCache()
    fills = [
        _fill(sid="bot1", side=OrderSide.BUY, qty=100.0, ts_ms=1000, event_key="e1"),
        _fill(sid="bot1", side=OrderSide.SELL, qty=50.0, ts_ms=2000, event_key="e2"),
    ]
    cache.bootstrap_from_fills("bot1", fills)
    rollup = cache.get_rollup("bot1")
    assert rollup.exposure.get("SPY", 0.0) == pytest.approx(50.0)
    assert rollup.last_activity_at_ms == 2000


# ── snapshot_all ──────────────────────────────────────────────────────────────


def test_snapshot_all_returns_all_bots() -> None:
    cache = BotRollupCache()
    for i in range(5):
        cache.on_fill_appended(_fill(sid=f"bot-{i}", event_key=f"e{i}"))
    snapshots = cache.snapshot_all()
    assert len(snapshots) == 5
    assert all(isinstance(v, BotRollup) for v in snapshots.values())


def test_known_sids() -> None:
    cache = BotRollupCache()
    cache.on_fill_appended(_fill(sid="bot-x", event_key="e1"))
    cache.on_fill_appended(_fill(sid="bot-y", event_key="e2"))
    assert set(cache.known_sids()) == {"bot-x", "bot-y"}


# ── ≥100-bot performance fixture (no O(journal) per-request scan) ─────────────


def test_100_bot_snapshot_no_journal_scan_per_request() -> None:
    """Demonstrates that snapshot_all is O(total_fills) not O(bots × journal).

    Acceptance criterion from spec §15 / issue #1296:
    "A ≥100-bot synthetic fixture must show NO O(journal) per-request scan."

    We populate 100 bots with 10 fills each (1000 total), then call
    snapshot_all and verify it completes in ≤200 ms (generous bound —
    actual should be well under 10 ms on any modern machine; the test
    guards against accidentally reintroducing full-journal reads on every
    get_rollup call).
    """
    cache = BotRollupCache()
    N_BOTS = 100
    FILLS_PER_BOT = 10

    for b in range(N_BOTS):
        sid = f"bot-{b:04d}"
        for f in range(FILLS_PER_BOT):
            ts = b * 10000 + f * 100
            cache.on_fill_appended(
                _fill(sid=sid, ts_ms=ts, event_key=f"exec:{b}:{f}")
            )

    start = time.monotonic()
    snapshots = cache.snapshot_all()
    elapsed_ms = (time.monotonic() - start) * 1000

    assert len(snapshots) == N_BOTS, f"expected {N_BOTS} bots; got {len(snapshots)}"
    assert elapsed_ms < 200, (
        f"snapshot_all took {elapsed_ms:.1f}ms for {N_BOTS} bots — "
        "suggests O(journal) per-request scan; expected < 200ms"
    )


# ── Thread-safety smoke ───────────────────────────────────────────────────────


def test_concurrent_appends_thread_safe() -> None:
    """Multiple threads can call on_fill_appended concurrently without crashing."""
    import threading

    cache = BotRollupCache()
    errors: list[Exception] = []

    def worker(thread_id: int) -> None:
        try:
            for i in range(20):
                cache.on_fill_appended(
                    _fill(sid="shared-bot", ts_ms=thread_id * 1000 + i, event_key=f"exec:{thread_id}:{i}")
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread-safety errors: {errors}"
    rollup = cache.get_rollup("shared-bot")
    # 5 threads × 20 appends each, all BUY → net positive exposure
    assert rollup.exposure.get("SPY", 0.0) > 0
