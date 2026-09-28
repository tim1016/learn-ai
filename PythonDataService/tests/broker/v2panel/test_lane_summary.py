"""A lane's own counts for its account card (PRD #2560): each counted alone, never a zero for unknown."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.broker_v2_panel import LaneAttentionItem, LaneAttentionRead
from app.services.broker_v2_panel import lane_summary


def _status(mode: str, running: bool) -> SimpleNamespace:
    return SimpleNamespace(mode=mode, running=running)


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        statuses=[_status("trade", True), _status("trade", True), _status("trade", False),
                  _status("dry_run", True), _status("log_only", True)],
        items=[LaneAttentionItem(condition_id="c", reason_code="EXIT_NOT_FLAT", severity="blocking", headline="Flatten")],
    )
    registry = SimpleNamespace(list_bots=lambda broker: state.statuses)

    async def attention() -> LaneAttentionRead:
        return LaneAttentionRead(account_id="PAPER", items=state.items)

    monkeypatch.setattr(lane_summary, "get_bot_task_registry", lambda: registry)
    monkeypatch.setattr(lane_summary, "lane_attention_read", attention)
    return state


async def test_lane_counts_are_the_lanes_own_facts(lane: SimpleNamespace) -> None:
    counts = await lane_summary.lane_counts()
    assert counts.reported() == {"running_count": 2, "dry_run_count": 1, "attention_count": 1}


async def test_a_count_that_cannot_be_taken_is_absent_and_the_rest_still_report(
    lane: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(broker: str) -> list:
        raise OSError("lifecycle file unreadable")

    monkeypatch.setattr(lane_summary, "get_bot_task_registry", lambda: SimpleNamespace(list_bots=broken))

    counts = await lane_summary.lane_counts()

    assert counts.reported() == {"attention_count": 1}
    assert any(getattr(record, "action", None) == "lane_count_unavailable" for record in caplog.records)
