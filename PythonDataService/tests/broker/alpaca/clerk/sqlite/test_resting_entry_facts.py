"""What an option entry keeps so that it can rest at the broker, and that older entries are untouched (#2913).

An entry accepted from now on keeps when the Clerk cancels what has not
filled, and the owner's order terms that time came from. An entry accepted
before an entry could rest has neither.

The pinned entry below was computed on this branch at ``0bc61e22``, before
the first line of the resting entry was written: two SPY 2026-09-18 585
calls at $1.94, admitted one second after noon. Its facts are hashed into the
custody chain and its payload hash is its identity, so neither may move by a
byte.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionEntryEvidence
from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.option_admission import OptionEntryRefused, work_out_option_entry
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts, option_entry_of_order
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.options.contract import OptionContract
from app.options.deadlines import CloseTerms
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.schemas.action_plan import ActionPlan
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.historical_run_fixture import seed_historical_run_started

_NOW = NOON + 1_000
_CALL = OptionContract.of(root="SPY", expiry_date=date(2026, 9, 18), right="call", strike=585)
_PLAN = ActionPlan.model_validate({
    "on_enter": [{
        "leg_id": "long_call", "instrument": {"kind": "option", "underlying": "SPY"}, "position": "long",
        "qty_ratio": 1, "right": "call", "strike": {"selector": "atm"}, "expiry": {"selector": "nearest_weekly"},
    }],
    "on_exit": [{"kind": "close_leg", "entry_leg_id": "long_call"}],
})

_PINNED_OPTION = (
    '{"admitted_at_ms":1788883201000,"cost_limit_percent":"10","leg_gap_limit_percent":"8","legs":[{"contract":'
    '{"expiry_ms":1789761600000,"right":"call","root":"SPY","strike_thousandths":585000},"leg_id":"long_call",'
    '"multiplier":100,"position":"long","quote":{"ask":"1.94","ask_received_ms":1788883200700,"bid":"1.90",'
    '"bid_received_ms":1788883200700,"data_type":"live","generation":4},"ratio":1}],"money":'
    '{"broker_requirement_cents":0,"entry_commitment_cents":38809,"fee_provision_cents":9,'
    '"loss_at_expiry_cents":38800,"net_entry_premium_cents":38800,"round_trip_cost_cents":800},'
    '"net_limit_price":"1.94","order":{"contract":{"expiry_ms":1789761600000,"right":"call","root":"SPY",'
    '"strike_thousandths":585000},"intent":"open","limit_price":"1.94","order_type":"limit","quantity":2,'
    '"side":"buy"},"quote_generation":4,"rechecked_at_ms":1788883200900,"units":2}'
)
_PINNED_PAYLOAD_HASH = "1c6a906a9df24fc661ed07d18166abf69f1c0f70dc60e9b6ff43af586d8ee5c9"
_PINNED_FACTS_JSON = (
    '{"action":"ENTER","decision_id":"d-1","effect_idempotency_key":"enter:spy-calls:d-1","effect_kind":"ENTER",'
    '"idempotency_key":"spy-calls:d-1","intended_end_state":null,"kind":"strategy_decision","leg":'
    '{"extended_hours":false,"limit_price":1.94,"order_type":"limit","quantity":2.0,"side":"buy",'
    '"symbol":"SPY260918C00585000","time_in_force":"day"},"option":' + _PINNED_OPTION
    + f',"payload_hash":"{_PINNED_PAYLOAD_HASH}"}}'
)


def _evidence(ask: str = "1.94", bid: str = "1.90", **changes: object) -> OptionEntryEvidence:
    quote = HeldQuote(
        bid=QuoteSide(Decimal(bid), _NOW - 300), ask=QuoteSide(Decimal(ask), _NOW - 300),
        data_type=QuoteDataType.LIVE, generation=4,
    )
    return replace(
        OptionEntryEvidence(
            picked={"long_call": _CALL}, multipliers={"long_call": 100}, quotes={"long_call": quote}, generation=4,
            rechecked_at_ms=_NOW - 100, leg_gap_limit_percent=Decimal(8), cost_limit_percent=Decimal(10),
        ),
        **changes,
    )


def _worked_out(evidence: OptionEntryEvidence) -> OptionEntryFacts:
    entry = work_out_option_entry(_PLAN, evidence=evidence, units=2, now_ms=_NOW)
    assert isinstance(entry, OptionEntryFacts), entry
    return entry


def _accepted(tmp_path: Path, entry: OptionEntryFacts) -> tuple[str, str, OptionEntryFacts | None]:
    """The stored facts, the identity hash and the entry read back, of ``entry`` accepted on fresh books."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path, clock=lambda: _NOW)
    try:
        repo.register_strategy_instance(strategy_instance_id="spy-calls", symbol="SPY", config_hash="h1")
        seed_historical_run_started(repo, account_id="PA-TEST", strategy_instance_id="spy-calls", lifecycle_run_id="run-1")
        accepted = accept_option_enter(
            repo, account_id="PA-TEST", strategy_instance_id="spy-calls", decision_id="d-1", lifecycle_run_id="run-1",
            entry=entry,
        )
        stored = repo.first_order_transition(order_ref=accepted.order_ref, transition_kind="ENTER_ACCEPTED")
        return stored["facts_json"], accepted.command.payload_hash, option_entry_of_order(repo, accepted.order_ref)
    finally:
        repo.close()


def test_an_entry_from_before_it_could_rest_is_stored_and_hashed_as_it_was(tmp_path: Path) -> None:
    before = OptionEntryFacts.from_facts(json.loads(_PINNED_OPTION))

    facts_json, payload_hash, read_back = _accepted(tmp_path, before)

    assert (before.cancel_at_ms, before.terms) == (None, None)
    assert canonicalize(before.to_facts()) == _PINNED_OPTION
    assert facts_json == _PINNED_FACTS_JSON
    assert payload_hash == _PINNED_PAYLOAD_HASH
    assert read_back == before


def test_a_new_entry_adds_its_cancel_time_and_terms_and_changes_nothing_else(tmp_path: Path) -> None:
    entry = _worked_out(_evidence())

    kept = entry.to_facts()
    added = {name: kept.pop(name) for name in ("cancel_at_ms", "terms")}

    assert canonicalize(kept) == _PINNED_OPTION
    # The Clerk's own clock when it admitted the entry, plus the owner's 20-second wait.
    assert added == {
        "cancel_at_ms": _NOW + 20_000,
        "terms": {
            "entry_wait_ms": 20_000, "cancel_allowance_ms": 30_000, "entry_margin_ms": 30_000,
            "expiry_close_lead_ms": 2_700_000, "sold_leg_close_lead_ms": 600_000,
        },
    }
    # The order is what it was, so the entry's identity is too.
    assert _accepted(tmp_path, entry)[1] == _PINNED_PAYLOAD_HASH


def test_an_entry_is_cancelled_after_the_wait_its_owner_set_and_keeps_those_terms(tmp_path: Path) -> None:
    owners = CloseTerms(entry_wait_ms=7_500, cancel_allowance_ms=11_000, entry_margin_ms=4_000)

    _, _, kept = _accepted(tmp_path, _worked_out(_evidence(terms=owners)))

    assert kept is not None and kept.terms is not None
    assert kept.cancel_at_ms == kept.admitted_at_ms + 7_500 == _NOW + 7_500
    assert kept.terms.close_terms == owners


def test_equal_prices_written_differently_make_one_order_and_one_identity(tmp_path: Path) -> None:
    plainly, with_a_trailing_zero = _worked_out(_evidence(ask="2.1", bid="2.06")), _worked_out(
        _evidence(ask="2.100", bid="2.0600")
    )

    assert plainly.order == with_a_trailing_zero.order
    assert plainly.order.model_dump(mode="json")["limit_price"] == "2.10"
    assert plainly.to_facts()["net_limit_price"] == with_a_trailing_zero.to_facts()["net_limit_price"] == "2.10"
    assert _accepted(tmp_path / "a", plainly)[1] == _accepted(tmp_path / "b", with_a_trailing_zero)[1]


def test_an_entry_handed_something_that_is_not_order_terms_is_refused() -> None:
    refused = work_out_option_entry(_PLAN, evidence=_evidence(terms={"entry_wait_ms": 20_000}), units=2, now_ms=_NOW)

    assert isinstance(refused, OptionEntryRefused)
    assert refused.reason_code == OptionEffectRefusal.ENTRY_EVIDENCE_INCOMPLETE.value


@pytest.mark.parametrize(
    "changed",
    [
        # A cancel time with no terms, terms with no cancel time, and a cancel time that is not the wait.
        {"cancel_at_ms": _NOW + 20_000},
        {"terms": {
            "entry_wait_ms": 20_000, "cancel_allowance_ms": 30_000, "entry_margin_ms": 30_000,
            "expiry_close_lead_ms": 2_700_000, "sold_leg_close_lead_ms": 600_000,
        }},
        {"cancel_at_ms": _NOW + 20_001, "terms": {
            "entry_wait_ms": 20_000, "cancel_allowance_ms": 30_000, "entry_margin_ms": 30_000,
            "expiry_close_lead_ms": 2_700_000, "sold_leg_close_lead_ms": 600_000,
        }},
    ],
)
def test_an_entry_whose_cancel_time_and_terms_do_not_agree_is_not_read_as_an_entry(changed: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="cancel time"):
        OptionEntryFacts.from_facts({**json.loads(_PINNED_OPTION), **changed})
