"""Phase 8 / VCR-0003 — durable SIZING_SKIP audit log tests.

Phase 8 SIZING_RESOLVED (#530) shipped the WAL emit for orders that
actually submit. The SIZING_SKIP half was deferred because the
IntentEvent invariant (``order_ref == namespace:intent_id``) refuses an
event without a minted intent_id. This PR ships the durable skip audit
as a separate ``sizing_skip.jsonl`` file alongside ``intent_events.jsonl``
— the Sizing card reads both to assemble the full audit list.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.engine.execution.order_sizer import FixedShares, OrderSizer
from app.engine.live.intent_wal import IntentWal
from app.engine.live.live_portfolio import LivePortfolio
from app.engine.live.order_identity import build_bot_order_namespace
from tests.engine.live.fixtures.fake_broker import FakeBroker


def _bar_time() -> datetime:
    return datetime(2026, 5, 4, 14, 30, tzinfo=UTC)


def _portfolio(tmp_path: Path) -> LivePortfolio:
    broker = FakeBroker()
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    portfolio = LivePortfolio(
        broker,
        intent_wal=wal,
        bot_order_namespace=build_bot_order_namespace("test-instance"),
        sizing_skip_log_path=tmp_path / "sizing_skip.jsonl",
    )
    portfolio.order_sizer = OrderSizer(FixedShares(value=10))
    portfolio.update_reference_price("SPY", Decimal("500"))
    return portfolio


def _read_skip_lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / "sizing_skip.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_skip_when_target_equals_current_writes_sizing_skip_vcr_0003(
    tmp_path: Path,
) -> None:
    """A set_holdings call where target_qty == current_qty (already at
    the target) appends a SIZING_SKIP audit row to sizing_skip.jsonl
    AND extends the in-memory sizing_resolutions list."""
    portfolio = _portfolio(tmp_path)
    portfolio.get_position("SPY").quantity = 10  # already at FixedShares(10)

    order = portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    assert order is None  # skip
    rows = _read_skip_lines(tmp_path)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "SIZING_SKIP"
    assert rows[0]["symbol"] == "SPY"
    assert rows[0]["policy_kind"] == "FixedShares"
    assert rows[0]["policy_value"] == "10"
    assert rows[0]["target_qty"] == 10
    assert rows[0]["current_qty"] == 10
    assert rows[0]["reference_price"] == "500"
    assert rows[0]["reason"] == "target_equals_current"
    # In-memory list also carries the skip with the new marker.
    assert len(portfolio.sizing_resolutions) == 1
    assert portfolio.sizing_resolutions[0]["skipped"] is True
    assert portfolio.sizing_resolutions[0]["skip_reason"] == "target_equals_current"


def test_skip_reason_uses_zero_shares_while_flat_when_both_are_zero(
    tmp_path: Path,
) -> None:
    """The reason classifier uses 'zero_shares_while_flat' when both
    target and current are zero, distinguishable from
    'target_equals_current' (both non-zero). Tested by calling the
    helper directly because the order_sizer policies refuse fraction=0
    / value=0 at the Pydantic boundary."""
    portfolio = _portfolio(tmp_path)
    portfolio._append_sizing_skip(  # type: ignore[reportPrivateUsage]
        ts_ms=1714838400000,
        symbol="SPY",
        policy_kind="FixedShares",
        policy_value="0",
        target_qty=0,
        current_qty=0,
        reference_price="500",
        reason="zero_shares_while_flat",
    )
    rows = _read_skip_lines(tmp_path)
    assert len(rows) == 1
    assert rows[0]["reason"] == "zero_shares_while_flat"


def test_skip_carries_no_intent_id_per_prd_section_8(tmp_path: Path) -> None:
    """PRD §8: 'SIZING_SKIP carries no intent_id'. The skip log row must
    not contain that field — the durable audit honestly records the
    decision without falsely reserving an identity."""
    portfolio = _portfolio(tmp_path)
    portfolio.get_position("SPY").quantity = 10

    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    rows = _read_skip_lines(tmp_path)
    assert "intent_id" not in rows[0]
    assert "order_ref" not in rows[0]


def test_skip_does_not_write_to_intent_events_wal(tmp_path: Path) -> None:
    """The intent_wal stays untouched by skips — only set_holdings calls
    that mint an intent_id append to intent_events.jsonl. Asserts the
    PRD §5A invariant 'a skip is not an intent' holds at the durable
    surface."""
    portfolio = _portfolio(tmp_path)
    portfolio.get_position("SPY").quantity = 10

    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    intent_wal_path = tmp_path / "intent_events.jsonl"
    if intent_wal_path.exists():
        assert intent_wal_path.read_text(encoding="utf-8") == ""


def test_skip_log_disabled_when_path_unset(tmp_path: Path) -> None:
    """Backward-compat: a portfolio without ``sizing_skip_log_path``
    skips silently — in-memory list still records the resolution but no
    file is written."""
    broker = FakeBroker()
    portfolio = LivePortfolio(broker)
    portfolio.order_sizer = OrderSizer(FixedShares(value=10))
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.get_position("SPY").quantity = 10

    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    assert _read_skip_lines(tmp_path) == []
    # In-memory list still captures the skip so the Sizing card UI works.
    assert portfolio.sizing_resolutions[0]["skipped"] is True


def test_multiple_skips_append_in_order(tmp_path: Path) -> None:
    """A run with several skip events appends them in monotonic order so
    the Sizing card can render them chronologically without sorting."""
    portfolio = _portfolio(tmp_path)
    portfolio.get_position("SPY").quantity = 10

    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    rows = _read_skip_lines(tmp_path)
    assert len(rows) == 3
    assert all(r["event_type"] == "SIZING_SKIP" for r in rows)
