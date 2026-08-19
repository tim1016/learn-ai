from __future__ import annotations

import re
from pathlib import Path
from typing import get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas.broker_v2_gallery import (
    GalleryBotDelta,
    GalleryBotView,
    GalleryLiveSnapshot,
    GalleryLiveUpdate,
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


def test_snapshot_round_trips_and_is_snake_case() -> None:
    snap = GalleryLiveSnapshot(
        stream_epoch="e1",
        surface_version=3,
        as_of_ms=1_700_000_060_000,
        resolution="5s",
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
                day_pnl=134.0,
                session_change_pct=0.05,
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


def test_snapshot_rejects_an_unsupported_bar_resolution() -> None:
    with pytest.raises(ValidationError):
        GalleryLiveSnapshot(
            stream_epoch="e1",
            surface_version=3,
            as_of_ms=1_700_000_060_000,
            resolution="15s",
            bots=[],
            symbols=[],
        )


def test_bot_delta_is_self_contained_with_symbol_and_label() -> None:
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
        day_pnl=147.0,
        session_change_pct=0.02,
        fills_today=13,
        last_bar_at_ms=1_700_000_120_000,
        primary_action=GalleryPrimaryAction(
            action_id="stop", label="Stop", enabled=True, disabled_reason=None
        ),
    )
    assert delta.symbol == "SPY"
    assert delta.label == "ORB"


_GALLERY_TYPES_TS_PATH = (
    Path(__file__).resolve().parents[3]
    / "Frontend"
    / "src"
    / "app"
    / "components"
    / "broker"
    / "v2-panel"
    / "gallery"
    / "lib"
    / "gallery.types.ts"
)

# GalleryBotDelta is a same-shape subclass of GalleryBotView (see its
# docstring: "carries the changed bot's full view"). The frontend
# deliberately has no separate delta type — it reuses GalleryBotView.
_PY_MODEL_TO_TS_TYPE_NAME = {"GalleryBotDelta": "GalleryBotView"}

_SCALAR_TS_TYPES = {int: "number", float: "number", str: "string", bool: "boolean"}


def _expected_ts_type(annotation: object) -> str:
    """Derive the TS type ``gallery.types.ts`` must declare for a Pydantic
    field annotation, so a backend type/nullability/nested-shape change that
    a bare field-name-set comparison would miss fails this test instead."""
    if annotation in _SCALAR_TS_TYPES:
        return _SCALAR_TS_TYPES[annotation]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _PY_MODEL_TO_TS_TYPE_NAME.get(annotation.__name__, annotation.__name__)
    origin = get_origin(annotation)
    if origin is list:
        (element,) = get_args(annotation)
        return f"readonly {_expected_ts_type(element)}[]"
    if origin is dict:
        key, value = get_args(annotation)
        assert key is str, f"only str-keyed dicts are supported by this test, got {annotation!r}"
        return f"Readonly<Record<string, {_expected_ts_type(value)}>>"
    raise AssertionError(f"no TS type mapping for {annotation!r} — extend this test")


def test_live_update_fields_match_the_pinned_frontend_type() -> None:
    """``GalleryLiveUpdate`` is SSE-only — it has no OpenAPI REST schema, so
    ``Frontend/src/app/components/broker/v2-panel/gallery/lib/gallery.types.ts``
    hand-declares it and pins to this model's field set instead of a
    generated alias (#1667). Keep both edits in the same commit.

    Compares each field's *type* (element/value shape, not just its name),
    not only the field-name set: a frontend-only rename, or a backend change
    to a field's type, container, or nested element type, must fail here —
    a bare ``set(keys) == set(keys)`` check cannot see any of that.

    Skipped when ``Frontend/`` isn't part of this checkout (e.g. the
    Python-only qualification container) — see
    ``test_python_and_frontend_snapshots_are_byte_identical`` in
    ``tests/broker/v2panel/test_vocabulary_snapshot.py`` for the same
    reasoning.
    """
    if not _GALLERY_TYPES_TS_PATH.exists():
        pytest.skip(f"Frontend/ not present in this checkout ({_GALLERY_TYPES_TS_PATH})")
    ts_source = _GALLERY_TYPES_TS_PATH.read_text(encoding="utf-8")
    interface_match = re.search(
        r"export interface GalleryLiveUpdate \{(?P<body>.*?)\n\}", ts_source, re.DOTALL
    )
    assert interface_match is not None, "GalleryLiveUpdate interface not found in gallery.types.ts"
    declared_fields = dict(
        re.findall(r"readonly (\w+):\s*([^;]+);", interface_match.group("body"))
    )

    assert set(GalleryLiveUpdate.model_fields.keys()) == set(declared_fields.keys())
    for name, field in GalleryLiveUpdate.model_fields.items():
        expected = _expected_ts_type(field.annotation)
        actual = " ".join(declared_fields[name].split())
        assert actual == expected, f"{name}: frontend declares `{actual}`, backend implies `{expected}`"
