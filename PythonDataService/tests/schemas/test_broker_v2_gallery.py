from __future__ import annotations

from app.schemas.broker_v2_gallery import (
    GalleryBotDelta,
    GalleryBotView,
    GalleryLiveSnapshot,
    GalleryPrimaryAction,
    GallerySymbolBars,
)
from app.schemas.broker_v2_panel import ChartBar


def _bar() -> ChartBar:
    return ChartBar(
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_060_000,
        open="1.0",
        high="1.2",
        low="0.9",
        close="1.1",
        volume=100,
        source="ibkr",
    )


def test_snapshot_round_trips_and_is_snake_case():
    snap = GalleryLiveSnapshot(
        stream_epoch="e1",
        surface_version=3,
        as_of_ms=1_700_000_060_000,
        resolution="1m",
        bots=[
            GalleryBotView(
                sid="Aug11-02",
                symbol="SPY",
                label="ORB",
                running=True,
                phase="ON_DUTY",
                desired_state="RUNNING",
                needs_attention=False,
                realized_pnl_today=142.0,
                open_pnl=-8.0,
                fills_today=12,
                last_bar_at_ms=1_700_000_060_000,
                primary_action=GalleryPrimaryAction(
                    action_id="stop", label="Stop", enabled=True, disabled_reason=None
                ),
            )
        ],
        symbols=[GallerySymbolBars(symbol="SPY", bars=[_bar()])],
        markers={"Aug11-02": []},
    )
    dumped = snap.model_dump()
    assert dumped["bots"][0]["realized_pnl_today"] == 142.0
    assert dumped["symbols"][0]["bars"][0]["start_ms"] == 1_700_000_000_000
    assert GalleryLiveSnapshot.model_validate(dumped).surface_version == 3


def test_bot_delta_is_self_contained_with_symbol_and_label():
    delta = GalleryBotDelta(
        sid="Aug11-02",
        symbol="SPY",
        label="ORB",
        running=True,
        phase="ON_DUTY",
        desired_state="RUNNING",
        needs_attention=False,
        realized_pnl_today=150.0,
        open_pnl=-3.0,
        fills_today=13,
        last_bar_at_ms=1_700_000_120_000,
        primary_action=GalleryPrimaryAction(
            action_id="stop", label="Stop", enabled=True, disabled_reason=None
        ),
    )
    assert delta.symbol == "SPY"
    assert delta.label == "ORB"
