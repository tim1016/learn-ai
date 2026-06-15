"""Phase 5E / VCR-0012 — cross-restart fill classifier.

``LiveEngine._convert_ibkr_fill`` used to drop any fill whose ``order_id``
wasn't in the in-memory ``_order_meta`` dict — a process restart wipes
that dict, so a fill that arrives for a perm_id owned by the prior
session was silently dropped, leaving the engine's portfolio out of sync
with the broker. Phase 5E adds the cross-restart classifier: when the
in-memory miss happens, the engine folds the durable intent WAL and
reconstructs the meta from the SubmittedOrderView whose perm_id matches.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from tests.engine.live.fixtures.fake_broker import FakeBroker


def _write_wal(tmp_path: Path, lines: list[dict]) -> Path:
    """Lay down an ``intent_events.jsonl`` of the given events."""
    path = tmp_path / "intent_events.jsonl"
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def _build_intent_event(
    *,
    seq: int,
    event_type: str,
    intent_id: str = "AAAAAAAAAAAAAAAAAAAAAA",
    namespace: str = "learn-ai/test-instance/v1",
    perm_id: int | None = None,
    order_id: int | None = None,
    order_spec: dict | None = None,
) -> dict:
    return {
        "seq": seq,
        "event_type": event_type,
        "intent_id": intent_id,
        "bot_order_namespace": namespace,
        "order_ref": f"{namespace}:{intent_id}",
        "intent_kind": "STRATEGY",
        "perm_id": perm_id,
        "order_id": order_id,
        "order_spec": order_spec,
    }


def _build_fill(
    *,
    order_id: int,
    perm_id: int | None,
    fill_quantity: float = 100.0,
    avg_fill_price: float = 500.0,
) -> IbkrOrderEvent:
    return IbkrOrderEvent(
        account_id="DU123",
        order_id=order_id,
        perm_id=perm_id,
        event_type="fill",
        status="Filled",
        exec_id=f"ex-{order_id}",
        client_id=1,
        fill_quantity=fill_quantity,
        avg_fill_price=avg_fill_price,
        last_fill_price=avg_fill_price,
        cumulative_filled=fill_quantity,
        remaining=0.0,
        exec_time_ms=1714838400000,
        fee=1.0,
        ts_ms=1714838400000,
    )


def _engine_with_wal(tmp_path: Path, *, wal_path: Path | None) -> LiveEngine:
    return LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        intent_wal_path=wal_path,
    )


def test_cross_restart_fill_classified_from_intent_wal_vcr_0012(
    tmp_path: Path,
) -> None:
    """A fill arriving for an unknown order_id but a perm_id known to the
    intent WAL gets reconstructed into an OrderEvent. This is the VCR-0012
    closure: prior session's bot order, fresh process — fill is NOT dropped."""
    wal_path = _write_wal(
        tmp_path,
        [
            _build_intent_event(
                seq=1,
                event_type="PENDING_INTENT",
                order_spec={
                    "symbol": "SPY",
                    "sec_type": "STK",
                    "action": "BUY",
                    "quantity": 100.0,
                    "order_type": "MKT",
                    "time_in_force": "DAY",
                    "confirm_paper": True,
                    "client_order_id": "live-1",
                    "order_ref": "learn-ai/test-instance/v1:AAAAAAAAAAAAAAAAAAAAAA",
                },
            ),
            _build_intent_event(
                seq=2,
                event_type="SUBMITTED",
                perm_id=999,
                order_id=1,
            ),
        ],
    )
    engine = _engine_with_wal(tmp_path, wal_path=wal_path)
    # Order_id 1 was placed by the prior session; this process's _order_meta
    # is empty so the lookup misses.
    assert 1 not in engine._order_meta

    fill = _build_fill(order_id=1, perm_id=999, fill_quantity=100.0)
    engine_event = engine._convert_ibkr_fill(fill)

    assert engine_event is not None
    assert engine_event.symbol == "SPY"
    assert engine_event.fill_quantity == 100  # BUY → positive
    assert engine_event.tag == "Phase5E:cross-restart"


def test_cross_restart_sell_signs_quantity_negative(tmp_path: Path) -> None:
    """A SELL order_spec produces a negative signed_qty on the reconstructed
    OrderEvent — the cross-restart classifier must respect order action."""
    wal_path = _write_wal(
        tmp_path,
        [
            _build_intent_event(
                seq=1,
                event_type="PENDING_INTENT",
                order_spec={
                    "symbol": "QQQ",
                    "sec_type": "STK",
                    "action": "SELL",
                    "quantity": 50.0,
                    "order_type": "MKT",
                    "time_in_force": "DAY",
                    "confirm_paper": True,
                    "client_order_id": "live-2",
                    "order_ref": "learn-ai/test-instance/v1:AAAAAAAAAAAAAAAAAAAAAA",
                },
            ),
            _build_intent_event(
                seq=2,
                event_type="SUBMITTED",
                perm_id=42,
                order_id=2,
            ),
        ],
    )
    engine = _engine_with_wal(tmp_path, wal_path=wal_path)
    fill = _build_fill(order_id=2, perm_id=42, fill_quantity=50.0)

    engine_event = engine._convert_ibkr_fill(fill)

    assert engine_event is not None
    assert engine_event.symbol == "QQQ"
    assert engine_event.fill_quantity == -50


def test_cross_restart_fill_drops_when_no_wal_path_set(tmp_path: Path) -> None:
    """When ``intent_wal_path`` is unset the engine has no durable surface to
    classify against — the original drop behavior holds."""
    engine = _engine_with_wal(tmp_path, wal_path=None)
    fill = _build_fill(order_id=99, perm_id=999, fill_quantity=10.0)

    assert engine._convert_ibkr_fill(fill) is None


def test_cross_restart_fill_drops_when_perm_id_not_in_wal(tmp_path: Path) -> None:
    """Genuinely foreign fill: perm_id is not in the WAL → drop."""
    wal_path = _write_wal(
        tmp_path,
        [
            _build_intent_event(
                seq=1,
                event_type="PENDING_INTENT",
                order_spec={
                    "symbol": "SPY",
                    "sec_type": "STK",
                    "action": "BUY",
                    "quantity": 100.0,
                    "order_type": "MKT",
                    "time_in_force": "DAY",
                    "confirm_paper": True,
                    "client_order_id": "live-3",
                    "order_ref": "learn-ai/test-instance/v1:AAAAAAAAAAAAAAAAAAAAAA",
                },
            ),
            _build_intent_event(seq=2, event_type="SUBMITTED", perm_id=111, order_id=3),
        ],
    )
    engine = _engine_with_wal(tmp_path, wal_path=wal_path)
    fill = _build_fill(order_id=3, perm_id=999, fill_quantity=100.0)  # different perm_id

    assert engine._convert_ibkr_fill(fill) is None


def test_cross_restart_fill_drops_when_fill_lacks_perm_id(tmp_path: Path) -> None:
    """Without a perm_id on the fill, the classifier has nothing to match
    against. The WAL fold path returns None and the warn-and-drop wins."""
    wal_path = _write_wal(
        tmp_path,
        [
            _build_intent_event(
                seq=1,
                event_type="PENDING_INTENT",
                order_spec={
                    "symbol": "SPY",
                    "sec_type": "STK",
                    "action": "BUY",
                    "quantity": 100.0,
                    "order_type": "MKT",
                    "time_in_force": "DAY",
                    "confirm_paper": True,
                    "client_order_id": "live-4",
                    "order_ref": "learn-ai/test-instance/v1:AAAAAAAAAAAAAAAAAAAAAA",
                },
            ),
            _build_intent_event(seq=2, event_type="SUBMITTED", perm_id=999, order_id=4),
        ],
    )
    engine = _engine_with_wal(tmp_path, wal_path=wal_path)
    fill = _build_fill(order_id=4, perm_id=None, fill_quantity=100.0)

    assert engine._convert_ibkr_fill(fill) is None


def test_cross_restart_classifier_skips_view_without_order_spec(
    tmp_path: Path,
) -> None:
    """A SUBMITTED event for a perm_id whose PENDING_INTENT has no order_spec
    (pre-Phase-5A WAL entry or legacy data) — the classifier safely returns
    None rather than guessing symbol or quantity."""
    wal_path = _write_wal(
        tmp_path,
        [
            _build_intent_event(seq=1, event_type="PENDING_INTENT"),
            _build_intent_event(seq=2, event_type="SUBMITTED", perm_id=777, order_id=5),
        ],
    )
    engine = _engine_with_wal(tmp_path, wal_path=wal_path)
    fill = _build_fill(order_id=5, perm_id=777, fill_quantity=100.0)

    assert engine._convert_ibkr_fill(fill) is None
