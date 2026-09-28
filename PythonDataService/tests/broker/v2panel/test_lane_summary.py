"""A lane's own counts for its account card (PRD #2560): each counted alone, never a zero for unknown."""

from __future__ import annotations

from collections import Counter
from dataclasses import fields
from types import SimpleNamespace

import pytest

from app.broker.fleet.records import LANE_COUNT_KEYS, ProviderSummaryObservation
from app.schemas.broker_v2_panel import LaneAttentionAction, LaneAttentionItem, LaneAttentionRead
from app.services.bot_runner import BotTaskRegistry
from app.services.broker_v2_panel import lane_summary

#: The real read, kept before any fixture replaces it.
_REAL_ATTENTION_READ = lane_summary.lane_attention_read


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        running=Counter({"trade": 2, "dry_run": 1, "log_only": 1}),
        items=[LaneAttentionItem(condition_id="c", reason_code="EXIT_NOT_FLAT", kind="exit", severity="blocking", headline="Flatten", strategy_instance_id="bot-1", action=LaneAttentionAction(label="Open bot", destination="bot"))],
    )
    registry = SimpleNamespace(running_mode_counts=lambda broker: state.running)

    async def attention() -> LaneAttentionRead:
        return LaneAttentionRead(account_id="PAPER", items=state.items)

    monkeypatch.setattr(lane_summary, "get_bot_task_registry", lambda: registry)
    monkeypatch.setattr(lane_summary, "lane_attention_read", attention)
    monkeypatch.setattr(lane_summary, "get_active_clerk_runtime", lambda: SimpleNamespace(sqlite_repository=object()))
    return state


async def test_lane_counts_are_the_lanes_own_facts(lane: SimpleNamespace) -> None:
    counts = await lane_summary.lane_counts()
    assert counts.reported() == {"running_count": 2, "dry_run_count": 1, "attention_count": 1}


async def test_a_count_that_cannot_be_taken_is_absent_and_the_rest_still_report(
    lane: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(broker: str) -> Counter[str]:
        raise RuntimeError("task table unreadable")

    monkeypatch.setattr(lane_summary, "get_bot_task_registry", lambda: SimpleNamespace(running_mode_counts=broken))

    counts = await lane_summary.lane_counts()

    assert counts.reported() == {"attention_count": 1}
    assert any(getattr(record, "action", None) == "lane_count_unavailable" for record in caplog.records)


async def test_a_lane_with_no_clerk_reports_attention_unknown_not_zero(lane: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review B2: the bell of a lane serving no account lists nothing, but
    that is "not counted" -- the card must not read "none need attention"."""
    monkeypatch.setattr(lane_summary, "lane_attention_read", _REAL_ATTENTION_READ)
    monkeypatch.setattr(lane_summary, "get_active_clerk_runtime", lambda: None)

    counts = await lane_summary.lane_counts()

    assert "attention_count" not in counts.reported()
    assert counts.reported() == {"running_count": 2, "dry_run_count": 1}


def test_running_counts_read_the_task_table_only(tmp_path) -> None:
    """Review B2: counting on every beat reads no binding or lifecycle file."""
    registry = BotTaskRegistry(tmp_path, feed_resolver=lambda: None, now_ms=lambda: 0, boot_recovery_required=False)

    def managed(broker: str, mode: str, *, done: bool) -> SimpleNamespace:
        return SimpleNamespace(binding=SimpleNamespace(broker=broker, mode=mode), task=SimpleNamespace(done=lambda: done))

    registry._bots.update({
        "a": managed("alpaca", "trade", done=False), "b": managed("alpaca", "trade", done=True),
        "c": managed("alpaca", "dry_run", done=False), "d": managed("ibkr", "trade", done=False),
    })

    assert registry.running_mode_counts("alpaca") == Counter({"trade": 1, "dry_run": 1})


def test_lane_count_keys_are_one_vocabulary_across_counter_wire_and_observation() -> None:
    """Review B1: the counter's fields, the wire keys and the coordinator's
    typed observation cannot drift apart."""
    counter = tuple(field.name for field in fields(lane_summary.LaneCounts))
    observation = tuple(field.name for field in fields(ProviderSummaryObservation) if field.name.endswith("_count"))
    assert counter == LANE_COUNT_KEYS == observation
    parsed = ProviderSummaryObservation.parse(
        {"endpoint_mode": "paper", "authority_state": "real_paper", **lane_summary.LaneCounts(1, 2, 3).reported()},
    )
    assert parsed is not None and parsed.counts() == {"running_count": 1, "dry_run_count": 2, "attention_count": 3}
