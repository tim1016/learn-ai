"""Offline fleet observations exercise collection without market or order access."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.broker.fleet.routing import LaneRouter, RoutedDelivery
from app.schemas.paper_live_experiments import ExperimentSource, PaperLiveEvidencePair
from app.services.paper_live_evidence_reader import ExperimentEvidenceUnavailable, PaperLiveEvidenceReader
from app.services.paper_live_evidence_store import PaperLiveEvidenceStore

BAR = 1_790_171_100_000


@pytest.fixture
def source() -> ExperimentSource:
    return ExperimentSource(
        clerk_id="paper-clerk",
        account_id="pa-test",
        binding_generation=2,
        db_identity_token="paper-db",
        strategy_instance_id="paper-bot",
    )


def delivery(
    source: ExperimentSource,
    *,
    after_seq: int = 0,
    highest_seq: int = 2,
    next_after_seq: int | None = None,
    seq: int = 1,
    outcome: str = "no_action",
    lane: str = "paper",
    **changes: object,
) -> RoutedDelivery:
    body = {
        "account_id": source.account_id.upper(),
        "strategy_instance_id": source.strategy_instance_id,
        "db_identity_token": source.db_identity_token,
        "authority_generation": 1,
        "config_hash": "sealed-config",
        "account_mode": lane,
        "authority_kind": "sqlite",
        "observed_at_ms": BAR + 5_000,
        "after_seq": after_seq,
        "highest_seq": highest_seq,
        "next_after_seq": next_after_seq,
        "decisions": [
            {
                "seq": seq,
                "run_id": f"{lane}-run",
                "recorded_at_ms": BAR + seq,
                "decision_bar_close_ms": BAR + (seq - 1) * 900_000,
                "trace_digest": "a" * 64,
                "outcome": outcome,
                "reason_code": "STRATEGY_DECISION",
            }
        ],
    } | changes
    return RoutedDelivery(
        status_code=200,
        headers={
            "X-Fleet-Broker": "alpaca",
            "X-Fleet-Clerk-Id": source.clerk_id,
            "X-Fleet-Binding-Generation": str(source.binding_generation),
        },
        body=json.dumps(body).encode(),
    )


async def test_reader_walks_every_page_with_fixed_watermark_and_restarts_at_zero(source: ExperimentSource) -> None:
    router = Mock(spec=LaneRouter)
    first = delivery(source, next_after_seq=1)
    last = delivery(source, seq=2, after_seq=1)
    router.deliver_read = AsyncMock(side_effect=[first, last, first, last])
    reader = PaperLiveEvidenceReader(router, clock=lambda: BAR + 10_000)
    for _ in range(2):
        capture = await reader.read(source, lane="paper")
        assert [d.seq for d in capture.decisions] == [1, 2]
        assert capture.highest_seq == 2
        assert capture.source == source  # The uppercase source account is the same canonical account.
        assert capture.captured_at_ms == BAR + 10_000
    requests = router.deliver_read.call_args_list
    assert [call.kwargs["query"] for call in requests] == [
        {"after_seq": "0", "limit": "500"},
        {"after_seq": "1", "through_seq": "2", "limit": "500"},
    ] * 2
    assert requests[0].kwargs["operation"].operation_id == "bot_decision_evidence"
    assert requests[0].kwargs["path_params"] == {"account_id": source.account_id, "sid": source.strategy_instance_id}
    router.deliver_command.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "pa-foreign"),
        ("strategy_instance_id", "foreign-bot"),
        ("db_identity_token", "restored-db"),
        ("account_mode", "live"),
        ("authority_kind", "shadow"),
    ],
)
async def test_reader_refuses_wrong_custody_or_execution_world(
    source: ExperimentSource, field: str, value: object
) -> None:
    router = Mock(spec=LaneRouter)
    router.deliver_read = AsyncMock(return_value=delivery(source, **{field: value}))
    with pytest.raises(ExperimentEvidenceUnavailable, match="source differs"):
        await PaperLiveEvidenceReader(router).read(source, lane="paper")


@pytest.mark.parametrize(
    "header,value",
    [
        ("X-Fleet-Broker", "foreign"),
        ("X-Fleet-Clerk-Id", "foreign-clerk"),
        ("X-Fleet-Binding-Generation", "3"),
        ("X-Fleet-Binding-Generation", None),
    ],
)
async def test_reader_requires_the_experiments_frozen_routing_identity(
    source: ExperimentSource,
    header: str,
    value: str | None,
) -> None:
    response = delivery(source)
    if value is None:
        response.headers.pop(header)
    else:
        response.headers[header] = value
    router = Mock(spec=LaneRouter)
    router.deliver_read = AsyncMock(return_value=response)
    with pytest.raises(ExperimentEvidenceUnavailable, match="routing identity"):
        await PaperLiveEvidenceReader(router).read(source, lane="paper")


@pytest.mark.parametrize(
    "field,value",
    [
        ("config_hash", "new-config"),
        ("authority_generation", 2),
        ("highest_seq", 3),
        ("after_seq", 0),
    ],
)
async def test_reader_refuses_mid_walk_changes(source: ExperimentSource, field: str, value: object) -> None:
    second = {"seq": 2, "after_seq": 1, field: value}
    router = Mock(spec=LaneRouter)
    router.deliver_read = AsyncMock(side_effect=[delivery(source, next_after_seq=1), delivery(source, **second)])
    with pytest.raises(ExperimentEvidenceUnavailable):
        await PaperLiveEvidenceReader(router).read(source, lane="paper")


async def test_reader_preserves_missing_prefix_instead_of_renumbering(source: ExperimentSource) -> None:
    router = Mock(spec=LaneRouter)
    router.deliver_read = AsyncMock(return_value=delivery(source, seq=8, highest_seq=8))
    capture = await PaperLiveEvidenceReader(router).read(source, lane="paper")
    assert capture.highest_seq == 8
    assert [r.seq for r in capture.decisions] == [8]


async def test_reader_refuses_malformed_page_and_unbounded_walk(
    source: ExperimentSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.paper_live_evidence_reader as module

    router = Mock(spec=LaneRouter)
    router.deliver_read = AsyncMock(return_value=delivery(source, next_after_seq=2))
    with pytest.raises(ExperimentEvidenceUnavailable, match="invalid decision evidence"):
        await PaperLiveEvidenceReader(router).read(source, lane="paper")
    monkeypatch.setattr(module, "_MAX_PAGES", 1)
    router.deliver_read.return_value = delivery(source, next_after_seq=1)
    with pytest.raises(ExperimentEvidenceUnavailable, match="page budget"):
        await PaperLiveEvidenceReader(router).read(source, lane="paper")


async def test_offline_entry_exit_collection_archives_both_lanes_without_broker_commands(
    source: ExperimentSource,
    tmp_path: Path,
) -> None:
    live = source.model_copy(
        update={
            "clerk_id": "live-clerk",
            "account_id": "live-account",
            "db_identity_token": "live-db",
            "strategy_instance_id": "live-bot",
        }
    )
    pair = PaperLiveEvidencePair(experiment_id="plx_" + "a" * 32, created_at_ms=BAR, paper=source, live=live)
    router = Mock(spec=LaneRouter)
    router.deliver_read = AsyncMock(
        side_effect=[
            delivery(source, outcome="entered", next_after_seq=1),
            delivery(source, seq=2, after_seq=1, outcome="exited"),
            delivery(live, lane="live", outcome="entered", next_after_seq=1),
            delivery(live, lane="live", seq=2, after_seq=1, outcome="exited"),
        ]
    )
    reader = PaperLiveEvidenceReader(router, clock=lambda: BAR + 10_000)
    with PaperLiveEvidenceStore.open(control_dir=tmp_path) as store:
        store.create(pair)
        for lane in ("paper", "live"):
            capture = await reader.read(getattr(pair, lane), lane=lane)
            report = store.capture(pair.experiment_id, lane, capture)
        assert report.comparison.all_decisions_match
        assert report.comparison.matching_decisions == 2
        assert [row.paper[0].outcome for row in report.comparison.rows] == ["entered", "exited"]
        assert report.comparison.sessions[0].live_run_ids == ("live-run",)
    router.deliver_command.assert_not_called()
