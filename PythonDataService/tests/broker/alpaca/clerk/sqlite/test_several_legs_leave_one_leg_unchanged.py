"""A one-bought-option ENTER is recorded exactly as it was before the Clerk took an entry of several legs (#2913).

Every value pinned here was computed on ``71d3e98a``, before the first line
of the several-legs entry was written. An entry of several legs is a new
type of facts beside the one-leg entry's, with a reservation of its own
arithmetic. A one-leg entry has neither: its facts, which are hashed into
the custody chain, its identity hash and its reservation row must not move
by a byte.
"""

from __future__ import annotations

from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (
    SID,
    _DryRun,
    dry_run,  # noqa: F401 -- pytest fixture, used by name
)

_HASH = "84686cce9f597bb8844f3274e25700fb27dfe9e8bae7e4d9afe3c2e96bfccf9e"
_FACTS = (
    '{"action":"ENTER","cash_reference_price":"1.94","decision_id":"enter-pinned","effect_idempotency_key":'
    '"enter:dry-run-buys-a-call:enter-pinned","effect_kind":"ENTER","fee_provision_cents":9,"idempotency_key":'
    '"dry-run-buys-a-call:enter-pinned","intended_end_state":null,"kind":"strategy_decision","leg":'
    '{"extended_hours":false,"limit_price":1.94,"order_type":"limit","quantity":2.0,"side":"buy","symbol":'
    '"SPY260918C00585000","time_in_force":"day"},"option":{"admitted_at_ms":1788883201000,"cancel_at_ms":'
    '1788883221000,"cost_limit_percent":"10","leg_gap_limit_percent":"8","legs":[{"contract":{"expiry_ms":'
    '1789761600000,"right":"call","root":"SPY","strike_thousandths":585000},"leg_id":"long_call","multiplier":100,'
    '"position":"long","quote":{"ask":"1.94","ask_received_ms":1788883200700,"bid":"1.90","bid_received_ms":'
    '1788883200700,"data_type":"live","generation":4},"ratio":1}],"money":{"broker_requirement_cents":0,'
    '"entry_commitment_cents":38809,"fee_provision_cents":9,"loss_at_expiry_cents":38800,"net_entry_premium_cents":'
    '38800,"round_trip_cost_cents":800},"net_limit_price":"1.94","order":{"contract":{"expiry_ms":1789761600000,'
    '"right":"call","root":"SPY","strike_thousandths":585000},"intent":"open","limit_price":"1.94","order_type":'
    '"limit","quantity":2,"side":"buy"},"quote_generation":4,"rechecked_at_ms":1788883200900,"terms":'
    '{"cancel_allowance_ms":30000,"entry_margin_ms":30000,"entry_wait_ms":20000,"expiry_close_lead_ms":2700000,'
    '"sold_leg_close_lead_ms":600000},"units":2},"payload_hash":"' + _HASH + '"}'
)


async def test_a_one_leg_option_enter_is_stored_with_the_facts_and_the_hash_it_had_before_several_legs(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    bought = await dry_run.enter(dry_run.evidence(), decision="enter-pinned", units=2)

    [order_ref] = bought.child_order_refs
    stored = dry_run.repo.first_order_transition(order_ref=order_ref, transition_kind="ENTER_ACCEPTED")
    command = dry_run.repo.get_command(f"cmd:{SID}:enter-pinned")
    assert stored["facts_json"] == _FACTS
    assert command is not None and command.payload_hash == _HASH


async def test_a_one_leg_option_enter_reserves_contracts_at_its_limit_as_it_did_before_several_legs(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence(), decision="enter-pinned", units=2)

    with dry_run.repo._write_lock:
        rows = [tuple(row) for row in dry_run.repo._conn.execute("SELECT * FROM envelope_reservations")]
        legs = dry_run.repo._conn.execute("SELECT COUNT(*) FROM option_order_legs").fetchone()[0]
    # Two contracts at $1.94 a share, and nine cents of fees: contracts and a price, as before.
    assert rows == [("effect:dry-run-buys-a-call:enter-pinned", 2.0, 1.94, 1788883201000, "1.94", 9)]
    # The table an entry of several legs keeps its legs in holds nothing of a one-leg entry.
    assert legs == 0
