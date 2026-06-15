"""Phase 5A / VCR-0002 — intent identity foundation tests.

The PRD's tracer-bullet contract:
``intent_id ↔ order_ref ↔ attempted broker order``. An ``intent_id`` is
minted only after sizing resolution proves the engine will submit
(``delta != 0``); a skip never mints one. Every submitted order stamps a
deterministic ``orderRef`` and emits ordered WAL events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrOrderAck, IbkrOrderSpec
from app.engine.execution.order_sizer import FixedShares, OrderSizer
from app.engine.live.intent_events import IntentEventType
from app.engine.live.intent_wal import IntentWal
from app.engine.live.live_portfolio import LivePortfolio
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    parse_order_ref,
)
from tests.engine.live.fixtures.fake_broker import FakeBroker


def _bar_time() -> datetime:
    return datetime(2026, 5, 4, 14, 30, tzinfo=UTC)


def _portfolio_with_intent_wal(tmp_path: Path, *, namespace: str = "test-instance") -> LivePortfolio:
    broker = FakeBroker()
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    portfolio = LivePortfolio(
        broker,
        intent_wal=wal,
        bot_order_namespace=build_bot_order_namespace(namespace),
    )
    portfolio.order_sizer = OrderSizer(FixedShares(value=10))
    portfolio.update_reference_price("SPY", Decimal("500"))
    return portfolio


def test_ibkr_order_spec_accepts_order_ref() -> None:
    """``IbkrOrderSpec`` gains an ``order_ref`` field so the deterministic
    namespace+intent_id token can ride to the broker."""
    spec = IbkrOrderSpec(
        symbol="SPY",
        sec_type="STK",
        action="BUY",
        quantity=10,
        order_type="MKT",
        time_in_force="DAY",
        confirm_paper=True,
        client_order_id="live-1",
        order_ref="learn-ai/test-instance/v1:AAAAAAAAAAAAAAAAAAAAAA",
    )
    assert spec.order_ref == "learn-ai/test-instance/v1:AAAAAAAAAAAAAAAAAAAAAA"


def test_set_holdings_with_zero_delta_does_not_mint_intent_id(tmp_path: Path) -> None:
    """A ``set_holdings`` call that resolves to ``target_qty == current_qty``
    is a no-op: nothing to submit, nothing to identify. The WAL stays empty,
    and the sizing card still records the resolution (the existing in-memory
    list is unchanged)."""
    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.get_position("SPY").quantity = 10  # already at FixedShares(10)

    order = portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    assert order is None
    assert portfolio.last_minted_intent_id() is None
    # WAL still empty — no PENDING_INTENT minted for a skip.
    wal_path = tmp_path / "intent_events.jsonl"
    assert not wal_path.exists() or wal_path.read_text() == ""
    # Sizing card audit row still appended (Phase 5A keeps the in-memory
    # list per PRD §5A; Phase 8 promotes to a WAL fold).
    assert len(portfolio.sizing_resolutions) == 1


def test_set_holdings_with_non_zero_delta_mints_intent_id(tmp_path: Path) -> None:
    """When ``delta != 0`` the engine is committing to a submit. ``intent_id``
    is minted now (not earlier) so a skip never reserves an identity."""
    portfolio = _portfolio_with_intent_wal(tmp_path)

    order = portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    assert order is not None
    intent_id = portfolio.last_minted_intent_id()
    assert intent_id is not None
    assert len(intent_id) == 22  # base64url uuid4 → 22 chars
    # The order carries the intent so ``submit_pending_orders`` can look it up.
    assert portfolio.intent_id_for_order(order.order_id) == intent_id


def test_submit_pending_orders_stamps_order_ref_on_spec(tmp_path: Path) -> None:
    """The broker sees ``IbkrOrderSpec.order_ref`` filled with the
    deterministic ``{namespace}:{intent_id}`` token. ``FakeBroker.orders``
    is the recorded boundary."""
    import asyncio

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    intent_id = portfolio.last_minted_intent_id()
    assert intent_id is not None

    asyncio.run(portfolio.submit_pending_orders())

    assert len(portfolio.broker.orders) == 1
    spec = portfolio.broker.orders[0]
    assert spec.order_ref is not None
    namespace, parsed_intent = parse_order_ref(spec.order_ref)
    assert namespace == build_bot_order_namespace("test-instance")
    assert parsed_intent == intent_id


def test_submit_pending_orders_writes_pending_intent_before_submit(tmp_path: Path) -> None:
    """The WAL invariant: ``PENDING_INTENT`` must be appended BEFORE
    ``broker.place_order`` is called. The broker fixture verifies the WAL
    file already contains the event when the submit lands."""
    import asyncio

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    intent_id = portfolio.last_minted_intent_id()

    wal_at_submit: list[str] = []
    original_place = portfolio.broker.place_order

    async def _capture(spec: IbkrOrderSpec, **kwargs: object) -> IbkrOrderAck:
        wal_at_submit.append((tmp_path / "intent_events.jsonl").read_text(encoding="utf-8"))
        return await original_place(spec, **kwargs)

    portfolio.broker.place_order = _capture  # type: ignore[assignment]
    asyncio.run(portfolio.submit_pending_orders())

    assert wal_at_submit, "broker should have been called"
    pending_line = wal_at_submit[0]
    assert IntentEventType.PENDING_INTENT.value in pending_line
    assert intent_id in pending_line


def test_submit_pending_orders_writes_submitted_after_success(tmp_path: Path) -> None:
    """On success, ``SUBMITTED`` is appended with the broker-side ``order_id``.
    The WAL pairs ``PENDING_INTENT → SUBMITTED`` with the same ``intent_id``."""
    import asyncio
    import json as _json

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    asyncio.run(portfolio.submit_pending_orders())

    wal_text = (tmp_path / "intent_events.jsonl").read_text(encoding="utf-8")
    events = [_json.loads(line) for line in wal_text.splitlines() if line.strip()]
    # Phase 8 inserts SIZING_RESOLVED ahead of PENDING_INTENT; the lifecycle
    # pair PENDING_INTENT → SUBMITTED still applies on the same intent_id.
    assert [e["event_type"] for e in events] == [
        IntentEventType.SIZING_RESOLVED.value,
        IntentEventType.PENDING_INTENT.value,
        IntentEventType.SUBMITTED.value,
    ]
    intent_id = events[0]["intent_id"]
    assert all(e["intent_id"] == intent_id for e in events)
    assert events[-1]["order_id"] is not None


def test_submit_pending_orders_writes_ack_failed_uncertain_on_exception(tmp_path: Path) -> None:
    """If ``broker.place_order`` raises, the submit path is uncertain — the
    order may or may not have landed at the broker. ``ACK_FAILED_UNCERTAIN``
    is the only honest event."""
    import asyncio
    import json as _json

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    async def _boom(spec: IbkrOrderSpec, **kwargs: object) -> IbkrOrderAck:
        raise RuntimeError("network reset by peer")

    portfolio.broker.place_order = _boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="network reset"):
        asyncio.run(portfolio.submit_pending_orders())

    wal_text = (tmp_path / "intent_events.jsonl").read_text(encoding="utf-8")
    events = [_json.loads(line) for line in wal_text.splitlines() if line.strip()]
    types = [e["event_type"] for e in events]
    assert IntentEventType.PENDING_INTENT.value in types
    assert IntentEventType.ACK_FAILED_UNCERTAIN.value in types
    uncertain = next(e for e in events if e["event_type"] == IntentEventType.ACK_FAILED_UNCERTAIN.value)
    assert "network reset" in (uncertain.get("reason") or "")


def test_legacy_portfolio_without_wal_keeps_working(tmp_path: Path) -> None:
    """Existing callers that don't pass an ``IntentWal`` keep their pre-Phase-5A
    behaviour — no intent_id minting, no WAL writes."""
    import asyncio

    broker = FakeBroker()
    portfolio = LivePortfolio(broker)
    portfolio.order_sizer = OrderSizer(FixedShares(value=10))
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    assert portfolio.last_minted_intent_id() is None
    asyncio.run(portfolio.submit_pending_orders())  # No WAL file expected
    assert not (tmp_path / "intent_events.jsonl").exists()


# ──────────────────────────────────────────────────────────────────────
# Phase 5B / VCR-0002 — durable-submit invariant tests.
#
# The Phase 5A surface above was opt-in via ``intent_wal is not None``. Phase 5B
# closes the structural hole: a broker adapter whose ``requires_durable_submit``
# marker is ``True`` CANNOT be wrapped in a ``LivePortfolio`` without an
# ``IntentWal`` + ``bot_order_namespace``. The WAL writes on the real-broker path
# are unconditional and the namespace match is asserted before placement.
# ──────────────────────────────────────────────────────────────────────


class _RealBrokerFake(FakeBroker):
    """FakeBroker subclass that declares itself a real-broker adapter for the
    invariant tests. Used wherever a test needs to exercise the Phase 5B
    code path without spinning up an actual IbkrBrokerAdapter."""

    requires_durable_submit = True


def test_real_broker_portfolio_without_intent_wal_raises() -> None:
    """ADR 0008 / Phase 5B — a real-broker LivePortfolio cannot be constructed
    without an IntentWal. Closes the bypass path VCR-0002 names: even after
    every wiring PR ships, ``intent_wal is None`` was the residual escape."""
    with pytest.raises(ValueError, match=r"ADR 0008.*IntentWal"):
        LivePortfolio(
            _RealBrokerFake(),
            bot_order_namespace="learn-ai/test-instance/v1",
        )


def test_real_broker_portfolio_without_namespace_raises(tmp_path: Path) -> None:
    """ADR 0008 / Phase 5B — a real-broker LivePortfolio cannot be constructed
    with an empty ``bot_order_namespace``: ownership identity is undefined
    without a namespace."""
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    with pytest.raises(ValueError, match=r"ADR 0008.*bot_order_namespace"):
        LivePortfolio(_RealBrokerFake(), intent_wal=wal)


def test_real_broker_portfolio_with_intent_wal_constructs(tmp_path: Path) -> None:
    """Happy path — the marker triggers the invariant, the invariant is
    satisfied, construction proceeds."""
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    portfolio = LivePortfolio(
        _RealBrokerFake(),
        intent_wal=wal,
        bot_order_namespace="learn-ai/test-instance/v1",
    )
    assert portfolio.intent_wal is wal
    assert portfolio.bot_order_namespace == "learn-ai/test-instance/v1"


def test_shadow_portfolio_still_works_without_intent_wal() -> None:
    """Shadow / fake adapters (no ``requires_durable_submit`` marker, or marker
    set to ``False``) retain the pre-Phase-5B opt-in behaviour: ``LivePortfolio``
    can be constructed without an IntentWal so existing replay / unit-test
    fixtures keep their shape."""
    portfolio = LivePortfolio(FakeBroker())  # FakeBroker has no marker → defaults to False
    assert portfolio.intent_wal is None
    assert portfolio.bot_order_namespace == ""


def test_real_broker_submit_writes_pending_intent_unconditionally(tmp_path: Path) -> None:
    """ADR 0008 / Phase 5B — on the real-broker code path, the WAL writes are
    unconditional. An order that did NOT go through ``set_holdings`` (e.g. a
    direct ``submit_market_order`` from a strategy or engine flatten path)
    still gets a minted intent_id, a stamped order_ref, and a fsynced
    PENDING_INTENT BEFORE ``broker.place_order`` is awaited."""
    import asyncio

    wal_path = tmp_path / "intent_events.jsonl"
    wal = IntentWal(wal_path)
    portfolio = LivePortfolio(
        _RealBrokerFake(),
        intent_wal=wal,
        bot_order_namespace=build_bot_order_namespace("test-instance"),
    )
    portfolio.update_reference_price("SPY", Decimal("500"))
    # Direct submit_market_order — bypasses set_holdings, so no intent_id was
    # minted upstream. The Phase 5B fallback mints one at submit time.
    portfolio.submit_market_order("SPY", 1, _bar_time(), tag="ManualEntry")

    asyncio.run(portfolio.submit_pending_orders())

    raw = wal_path.read_text(encoding="utf-8").splitlines()
    assert raw, "WAL must contain at least one event"
    # First event is PENDING_INTENT, second is SUBMITTED — same intent_id.
    import json

    events = [json.loads(line) for line in raw if line.strip()]
    assert [e["event_type"] for e in events] == [
        IntentEventType.PENDING_INTENT.value,
        IntentEventType.SUBMITTED.value,
    ]
    assert events[0]["intent_id"] == events[1]["intent_id"]
    assert events[0]["order_ref"].startswith(build_bot_order_namespace("test-instance") + ":")
    # The broker saw a non-empty order_ref on its spec.
    assert portfolio.broker.orders[0].order_ref is not None
    assert portfolio.broker.orders[0].order_ref == events[0]["order_ref"]


def test_real_broker_namespace_mismatch_assertion_fires(tmp_path: Path, monkeypatch) -> None:
    """ADR 0008 / Phase 5B — defense-in-depth. If a future bug supplied an
    ``order_ref`` that does not match this instance's ``bot_order_namespace``
    (e.g. stale value from cold-start adoption or a cross-instance leak),
    ``submit_pending_orders`` refuses before ``broker.place_order`` is
    awaited."""
    import asyncio

    wal = IntentWal(tmp_path / "intent_events.jsonl")
    portfolio = LivePortfolio(
        _RealBrokerFake(),
        intent_wal=wal,
        bot_order_namespace=build_bot_order_namespace("test-instance"),
    )
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.submit_market_order("SPY", 1, _bar_time())

    # Force build_order_ref (called inside submit_pending_orders) to return a
    # token whose namespace does NOT match the portfolio's bot_order_namespace.
    # The defense-in-depth assertion must fire before the broker is hit.
    def _wrong_namespace(_ns: str, intent_id: str) -> str:
        return f"learn-ai/SOMEONE-ELSE/v1:{intent_id}"

    monkeypatch.setattr(
        "app.engine.live.order_identity.build_order_ref", _wrong_namespace
    )

    with pytest.raises(AssertionError, match="ADR 0008 namespace mismatch"):
        asyncio.run(portfolio.submit_pending_orders())
    # And the broker must never have been invoked.
    assert portfolio.broker.orders == []


# ---------------------------------------------------------------------------
# Phase 8 (VCR-0003) — SIZING_RESOLVED WAL emission
# ---------------------------------------------------------------------------


def _read_wal_events(tmp_path: Path) -> list[dict]:
    import json

    path = tmp_path / "intent_events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_set_holdings_appends_sizing_resolved_with_minted_intent_id(tmp_path: Path) -> None:
    """Phase 8 — after a non-skip ``set_holdings`` the WAL holds a single
    SIZING_RESOLVED event keyed to the freshly-minted intent_id."""
    portfolio = _portfolio_with_intent_wal(tmp_path)

    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    intent_id = portfolio.last_minted_intent_id()
    assert intent_id is not None

    events = _read_wal_events(tmp_path)
    sizing = [e for e in events if e["event_type"] == IntentEventType.SIZING_RESOLVED.value]
    assert len(sizing) == 1
    row = sizing[0]
    assert row["intent_id"] == intent_id
    assert row["order_ref"] == f"learn-ai/test-instance/v1:{intent_id}"
    assert row["policy_kind"] == "FixedShares"
    assert row["policy_value"] == "10"
    assert row["intended_qty"] == 10
    assert row["reference_price"] == "500"
    assert row["sizing_provenance_at_resolve_time"] == "live_override"
    assert row["sized_via"] == "policy_set_holdings"


def test_set_holdings_skip_writes_no_sizing_resolved(tmp_path: Path) -> None:
    """Phase 8 — a skip (delta == 0) must NOT mint an intent_id and therefore
    must NOT append SIZING_RESOLVED. SIZING_SKIP (which carries no intent_id)
    is the deferred half tracked as VCR-0003 follow-up."""
    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.get_position("SPY").quantity = 10  # already at FixedShares(10)

    order = portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    assert order is None
    events = _read_wal_events(tmp_path)
    assert all(e["event_type"] != IntentEventType.SIZING_RESOLVED.value for e in events)


def test_set_holdings_sizing_resolved_precedes_pending_intent(tmp_path: Path) -> None:
    """Phase 8 / PRD §8 — SIZING_RESOLVED must be appended BEFORE the
    PENDING_INTENT that follows in the submit path. The fold relies on this
    ordering when joining the Sizing card to the trade record."""
    import asyncio

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    asyncio.run(portfolio.submit_pending_orders())

    events = _read_wal_events(tmp_path)
    types = [e["event_type"] for e in events]
    assert IntentEventType.SIZING_RESOLVED.value in types
    assert IntentEventType.PENDING_INTENT.value in types
    sizing_idx = types.index(IntentEventType.SIZING_RESOLVED.value)
    pending_idx = types.index(IntentEventType.PENDING_INTENT.value)
    assert sizing_idx < pending_idx


# ---------------------------------------------------------------------------
# Phase 5D (VCR-0002) — submit retry policy via submit_state_machine
# ---------------------------------------------------------------------------


def _make_probing_broker(
    portfolio: LivePortfolio,
    *,
    boom_count: int,
    probe_returns: str,
) -> list[str]:
    """Wire ``portfolio.broker`` so that the first ``boom_count`` place_order
    calls raise, then succeed. The broker's probe_intent_status always
    returns ``probe_returns``. Returns a list that records each broker call
    in order so tests can assert the state-machine path taken."""
    timeline: list[str] = []
    remaining_booms = boom_count
    original_place = portfolio.broker.place_order

    async def _capture_place(spec: IbkrOrderSpec, **kwargs: object) -> IbkrOrderAck:
        nonlocal remaining_booms
        timeline.append(f"place:{spec.order_ref}")
        if remaining_booms > 0:
            remaining_booms -= 1
            raise RuntimeError("synthetic broker outage")
        return await original_place(spec, **kwargs)

    async def _probe(intent_id: str, order_ref: str) -> str:
        timeline.append(f"probe:{order_ref}")
        return probe_returns

    portfolio.broker.place_order = _capture_place  # type: ignore[assignment]
    portfolio.broker.probe_intent_status = _probe  # type: ignore[assignment]
    return timeline


def test_submit_halts_on_not_provable_probe_vcr_0002(tmp_path: Path) -> None:
    """Phase 5D — NOT_PROVABLE is the default. The state machine HALTs on
    the first uncertain ack rather than guess; the WAL records
    SUBMIT_UNCERTAIN_HALTED and the runner raises SubmitUncertainHaltError."""
    import asyncio

    from app.engine.live.live_portfolio import SubmitUncertainHaltError

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    timeline = _make_probing_broker(portfolio, boom_count=1, probe_returns="NOT_PROVABLE")

    with pytest.raises(SubmitUncertainHaltError) as exc:
        asyncio.run(portfolio.submit_pending_orders())

    assert exc.value.probe_result == "NOT_PROVABLE"
    assert exc.value.retry_count == 0
    types = [e["event_type"] for e in _read_wal_events(tmp_path)]
    assert IntentEventType.SUBMIT_UNCERTAIN_HALTED.value in types
    # No retry attempted: only one place call, one probe.
    assert sum(1 for t in timeline if t.startswith("place:")) == 1
    assert sum(1 for t in timeline if t.startswith("probe:")) == 1


def test_submit_retries_once_then_succeeds_on_provably_absent_vcr_0002(
    tmp_path: Path,
) -> None:
    """Phase 5D — PROVABLY_ABSENT with retry_count < RETRY_CAP retries
    with the SAME intent_id/order_ref. The replaced order succeeds and the
    WAL carries [PENDING_INTENT, ACK_FAILED_UNCERTAIN, INTENT_NOT_ACCEPTED,
    PENDING_INTENT, SUBMITTED] all sharing the same intent_id."""
    import asyncio

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    intent_id = portfolio.last_minted_intent_id()
    assert intent_id is not None
    timeline = _make_probing_broker(
        portfolio, boom_count=1, probe_returns="PROVABLY_ABSENT"
    )

    asyncio.run(portfolio.submit_pending_orders())

    events = _read_wal_events(tmp_path)
    types = [e["event_type"] for e in events]
    # SIZING_RESOLVED is Phase 8's prelude; assert the Phase 5D lifecycle.
    assert types[-5:] == [
        IntentEventType.PENDING_INTENT.value,
        IntentEventType.ACK_FAILED_UNCERTAIN.value,
        IntentEventType.INTENT_NOT_ACCEPTED.value,
        IntentEventType.PENDING_INTENT.value,
        IntentEventType.SUBMITTED.value,
    ]
    # Every event carries the SAME intent_id (no fresh mint on retry).
    submit_events = [e for e in events if e["event_type"] != IntentEventType.SIZING_RESOLVED.value or e["intent_id"] == intent_id]
    assert all(e["intent_id"] == intent_id for e in submit_events)
    # Two place calls (initial + retry) and one probe.
    assert sum(1 for t in timeline if t.startswith("place:")) == 2
    assert sum(1 for t in timeline if t.startswith("probe:")) == 1


def test_submit_halts_after_retry_cap_on_provably_absent_vcr_0002(
    tmp_path: Path,
) -> None:
    """Phase 5D — RETRY_CAP = 1. A second PROVABLY_ABSENT halts; the WAL
    records [..., INTENT_NOT_ACCEPTED, PENDING_INTENT, ACK_FAILED_UNCERTAIN,
    SUBMIT_UNCERTAIN_HALTED] and SubmitUncertainHaltError carries
    retry_count >= 1."""
    import asyncio

    from app.engine.live.live_portfolio import SubmitUncertainHaltError

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    timeline = _make_probing_broker(
        portfolio, boom_count=2, probe_returns="PROVABLY_ABSENT"
    )

    with pytest.raises(SubmitUncertainHaltError) as exc:
        asyncio.run(portfolio.submit_pending_orders())

    assert exc.value.retry_count == 1  # crossed the cap on second attempt
    types = [e["event_type"] for e in _read_wal_events(tmp_path)]
    assert types.count(IntentEventType.PENDING_INTENT.value) == 2
    assert types.count(IntentEventType.ACK_FAILED_UNCERTAIN.value) == 2
    assert types[-1] == IntentEventType.SUBMIT_UNCERTAIN_HALTED.value
    assert sum(1 for t in timeline if t.startswith("place:")) == 2
    assert sum(1 for t in timeline if t.startswith("probe:")) == 2


def test_submit_adopts_on_present_probe_vcr_0002(tmp_path: Path) -> None:
    """Phase 5D — PRESENT means the broker has the order. Record
    SUBMITTED_RECOVERED and stop; no second place_order call."""
    import asyncio

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    timeline = _make_probing_broker(portfolio, boom_count=1, probe_returns="PRESENT")

    acks = asyncio.run(portfolio.submit_pending_orders())

    # No ack synthesized for adopted orders — broker fills will reconcile
    # against the WAL.
    assert acks == []
    types = [e["event_type"] for e in _read_wal_events(tmp_path)]
    assert types[-1] == IntentEventType.SUBMITTED_RECOVERED.value
    # Only ONE place call: the broker already has it, no re-place.
    assert sum(1 for t in timeline if t.startswith("place:")) == 1
    assert sum(1 for t in timeline if t.startswith("probe:")) == 1
