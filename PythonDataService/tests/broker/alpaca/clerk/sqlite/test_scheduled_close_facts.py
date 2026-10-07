"""What a scheduled close keeps on its acceptance, and that every other EXIT is written as it was (#2913).

A scheduled close's ``EXIT_ACCEPTED`` gains one key, ``scheduled_close``:
which close it is, when it was due, and the session it was due in. The log is
hash-chained, so the key is left out of every acceptance that is not one.

The five acceptances pinned below were computed on this branch at
``8084cd75``, before the first line of the scheduled close was written.
"""

from __future__ import annotations

import json

import pytest

from app.broker.alpaca.clerk.program_leg import LegShape
from app.broker.alpaca.clerk.sqlite.facts import ExitAcceptedFacts, ScheduledCloseFacts
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from app.options.deadlines import CloseKind
from app.schemas.market_liveness import TopOfBookQuote

_HASH = "a" * 64
_ENTRY = "learn-ai-bot-1-entry"
_LIMIT = LegShape(
    order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, limit_price=99.5, extended_hours=True,
    side=OrderSide.SELL,
)
_QUOTE = TopOfBookQuote(
    symbol="SPY", bid=99.6, ask=99.8, source="ibkr.market_data.status", observed_at_ms=1_788_883_200_000
)


def _accepted(decision_id: str, **more: object) -> ExitAcceptedFacts:
    return ExitAcceptedFacts(
        idempotency_key=f"spy-bot:{decision_id}", payload_hash=_HASH, kind="strategy_decision", action="EXIT",
        intended_end_state=None, effect_idempotency_key=f"exit:spy-bot:{decision_id}", effect_kind="EXIT",
        decision_id=decision_id, entry_order_ref=_ENTRY, entry_order_refs=[_ENTRY], **more,  # type: ignore[arg-type]
    )


def _common(decision_id: str) -> str:
    return (
        f'"decision_id":"{decision_id}","effect_idempotency_key":"exit:spy-bot:{decision_id}","effect_kind":"EXIT",'
        f'"entry_order_ref":"{_ENTRY}","entry_order_refs":["{_ENTRY}"],'
    )


_PRICED = (
    '"reducing_side":"sell","reducing_valid_until_ms":1788890400000,"reference_ask":99.8,"reference_bid":99.6,'
    '"reference_quote_observed_at_ms":1788883200000}'
)

_PINNED: dict[str, tuple[ExitAcceptedFacts, str]] = {
    "a deciding program's exit in regular hours": (
        _accepted("d-1"),
        '{"action":"EXIT",' + _common("d-1")
        + f'"idempotency_key":"spy-bot:d-1","intended_end_state":null,"kind":"strategy_decision","payload_hash":"{_HASH}"}}',
    ),
    "the sale at a bot's scheduled end": (
        _accepted("scheduled-end-0123456789abcdef", regular_session_only=True),
        '{"action":"EXIT",' + _common("scheduled-end-0123456789abcdef")
        + '"idempotency_key":"spy-bot:scheduled-end-0123456789abcdef","intended_end_state":null,'
        f'"kind":"strategy_decision","payload_hash":"{_HASH}","regular_session_only":true}}',
    ),
    "an option close with the quotes its decision read": (
        _accepted(
            "d-2", regular_session_only=True, option_contract="SPY260918C00585000",
            option_exit={
                "quotes": {"SPY260918C00585000": {"bid": "1.90", "ask": "1.94"}}, "generation": 4,
                "read_at_ms": 1_788_883_200_000,
            },
        ),
        '{"action":"EXIT",' + _common("d-2")
        + '"idempotency_key":"spy-bot:d-2","intended_end_state":null,"kind":"strategy_decision",'
        '"option_contract":"SPY260918C00585000","option_exit":{"generation":4,"quotes":{"SPY260918C00585000":'
        '{"ask":"1.94","bid":"1.90"}},"read_at_ms":1788883200000},'
        f'"payload_hash":"{_HASH}","regular_session_only":true}}',
    ),
    "the watchdog's re-drive at a price the Clerk set": (
        _accepted("exit-redrive-0123456789ab-1").with_reducing_shape(
            _LIMIT, valid_until_ms=1_788_890_400_000, reference_quote=_QUOTE, confirmed_quantity=10.0,
            priced_by="clerk",
        ),
        '{"action":"EXIT",' + _common("exit-redrive-0123456789ab-1")
        + '"extended_hours":true,"idempotency_key":"spy-bot:exit-redrive-0123456789ab-1","intended_end_state":null,'
        f'"kind":"strategy_decision","limit_price":99.5,"order_type":"limit","payload_hash":"{_HASH}",'
        '"reducing_confirmed_quantity":10.0,"reducing_priced_by":"clerk",' + _PRICED,
    ),
    "an operator's flatten at a confirmed limit outside the band": (
        _accepted("recovery-flatten-0123456789abcdef").with_reducing_shape(
            _LIMIT, valid_until_ms=1_788_890_400_000, reference_quote=_QUOTE, confirmed_quantity=10.0,
            priced_by="operator", band_override=True,
        ),
        '{"action":"EXIT","band_override":true,' + _common("recovery-flatten-0123456789abcdef")
        + '"extended_hours":true,"idempotency_key":"spy-bot:recovery-flatten-0123456789abcdef",'
        '"intended_end_state":null,"kind":"strategy_decision","limit_price":99.5,"order_type":"limit",'
        f'"payload_hash":"{_HASH}","reducing_confirmed_quantity":10.0,' + _PRICED,
    ),
}


@pytest.mark.parametrize("which", list(_PINNED))
def test_an_exit_that_is_no_scheduled_close_is_written_byte_for_byte_as_it_was(which: str) -> None:
    accepted, pinned = _PINNED[which]

    assert accepted.to_facts_json() == pinned
    # And read back from those bytes, it writes the same bytes again.
    assert ExitAcceptedFacts.from_facts_json(pinned).to_facts_json() == pinned
    assert ExitAcceptedFacts.from_facts_json(pinned).the_scheduled_close is None


def test_a_scheduled_close_keeps_which_close_it_is_when_it_was_due_and_its_session() -> None:
    close = ScheduledCloseFacts(
        kind=CloseKind.EXPIRY_DAY.value, due_ms=1_789_758_900_000, session_open_ms=1_789_738_200_000
    )
    plain, _pinned = _PINNED["an option close with the quotes its decision read"]

    written = ExitAcceptedFacts(**{**plain.__dict__, "scheduled_close": close.to_mapping()}).to_facts_json()

    # One key more than the same EXIT without it, and nothing else moved.
    assert json.loads(written) == {
        **json.loads(plain.to_facts_json()),
        "scheduled_close": {"kind": "expiry_day_close", "due_ms": 1_789_758_900_000, "session_open_ms": 1_789_738_200_000},
    }
    assert ExitAcceptedFacts.from_facts_json(written).the_scheduled_close == close


@pytest.mark.parametrize(
    "kept",
    [
        pytest.param({"kind": "expiry_day_close", "due_ms": 1}, id="a key missing"),
        pytest.param({"kind": "expiry_day_close", "due_ms": 1, "session_open_ms": 1, "late": True}, id="a key too many"),
        pytest.param({"kind": "", "due_ms": 1, "session_open_ms": 1}, id="no kind"),
        pytest.param({"kind": "expiry_day_close", "due_ms": "1", "session_open_ms": 1}, id="a time that is text"),
        pytest.param({"kind": "expiry_day_close", "due_ms": True, "session_open_ms": 1}, id="a time that is a flag"),
        pytest.param({"kind": "expiry_day_close", "due_ms": -1, "session_open_ms": 1}, id="a time before the epoch"),
        pytest.param(["expiry_day_close", 1, 1], id="not an object"),
    ],
)
def test_a_scheduled_close_that_cannot_be_read_back_whole_is_refused(kept: object) -> None:
    with pytest.raises(ValueError, match="scheduled close"):
        ScheduledCloseFacts.from_mapping(kept)
