"""A simulated structure fills every leg together, with exact retained custody (#2913)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding, entry_outcome
from app.broker.alpaca.clerk.sqlite.option_decision_fills import option_fills_of_decision
from app.broker.alpaca.clerk.sqlite.option_entry import MultiLegOptionEntryFacts
from app.broker.alpaca.clerk.sqlite.option_order_evidence import (
    multi_leg_order_evidence,
    with_the_simulations_own_leg_records,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_submission_response
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.errors import BrokerOrderRejected
from app.options.simulated_fill import SimulatedFillModel
from app.schemas.action_plan import ActionPlan
from app.services.source_bar_ledger import SourceBarLedger
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import ACCOUNT_ID, RUN_ID, open_option_books
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import (
    PAID_PICKS,
    PAID_SPREAD,
    RECEIVED_PICKS,
    RECEIVED_SPREAD,
    STRADDLE,
    STRADDLE_PICKS,
    Pick,
    call,
    plan,
    put,
    worked_entry,
)
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import (
    leg as plan_leg,
)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[ClerkSqliteRepository, SyntheticBroker]]:
    books = open_option_books(tmp_path, bots={"bot-a": "SPY", "bot-b": "SPY"})
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:several-legs")
    try:
        yield books, SyntheticBroker(account_id="sim:several-legs", source_bars=bars, clock=lambda: NOON)
    finally:
        books.close()
        bars.close()


def _bind(port: SyntheticBroker, parent_ref: str, entry: MultiLegOptionEntryFacts, *, omit: str | None = None) -> None:
    for leg in entry.legs:
        if leg.leg_id != omit:
            port.bind_option_entry_quote(
                parent_ref, symbol=leg.contract.occ_symbol, quote=leg.quote.held, multiplier=leg.multiplier,
            )


@pytest.mark.parametrize(
    ("plan", "picks", "net"),
    [(PAID_SPREAD, PAID_PICKS, "1.94"), (RECEIVED_SPREAD, RECEIVED_PICKS, "-3.07"),
     (STRADDLE, STRADDLE_PICKS, "6.00")],
    ids=["194 dollars paid", "307 dollars received", "two bought contracts"],
)
async def test_every_leg_is_filled_once_and_the_clerk_counts_whole_units(
    world: tuple[ClerkSqliteRepository, SyntheticBroker], plan: ActionPlan, picks: Mapping[str, Pick], net: str,
) -> None:
    books, port = world
    entry = worked_entry(plan, picks, units=2, now_ms=NOON)
    accepted = accept_option_enter(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="d1", lifecycle_run_id=RUN_ID,
        entry=entry,
    )
    assert accepted.order_ref is not None
    _bind(port, accepted.order_ref, entry)
    answer = await port.submit_option_order(entry.order, client_order_id=accepted.order_ref)
    assert (answer.status, answer.quantity, answer.filled_quantity, answer.filled_avg_price) == (
        "filled", 2, 2, Decimal(net),
    )
    evidence = await with_the_simulations_own_leg_records(port, multi_leg_order_evidence(answer))
    for _ in range(2):
        fold_order_submission_response(books, effect_operation_id=accepted.effect_operation_id, order=evidence, trade=port)
    outcome = entry_outcome(books, accepted.order_ref)
    assert outcome.standing is EntryStanding.FILLED and outcome.proven
    assert (outcome.units_asked, outcome.units_filled, outcome.legs_out_of_step) == (2, 2, False)
    assert outcome.contracts_filled == {leg.contract.occ_symbol: 2 for leg in entry.legs}
    assert len(outcome.order_refs) == 3 and books.fills_for_order(accepted.order_ref) == []
    fills = option_fills_of_decision(
        books, strategy_instance_id="bot-a", durable_decision_id="d1", how_simulated=port.synthesized_option_fills(),
    )
    assert len(fills) == 2
    assert all(fill.quantity == 2 and fill.multiplier == 100 and fill.quote_age_ms == 300 for fill in fills)
    assert all(fill.fill_model == SimulatedFillModel.NATURAL_PRICE.value for fill in fills)
    assert [(fill.contract, fill.side) for fill in fills] == [
        (leg.contract.occ_symbol, "buy" if leg.position == "long" else "sell") for leg in entry.legs
    ]
    looked_up = await port.get_option_order_by_client_order_id(accepted.order_ref)
    assert looked_up == answer
    again = await port.submit_option_order(entry.order, client_order_id=accepted.order_ref)
    assert again == answer and len(port.synthesized_option_fills()) == 2


async def test_one_missing_quote_refuses_the_whole_structure_and_records_no_leg(
    world: tuple[ClerkSqliteRepository, SyntheticBroker],
) -> None:
    _books, port = world
    entry = worked_entry(PAID_SPREAD, PAID_PICKS, units=2, now_ms=NOON)
    _bind(port, "spread", entry, omit="short_call")
    with pytest.raises(BrokerOrderRejected, match=entry.legs[1].contract.occ_symbol):
        await port.submit_option_order(entry.order, client_order_id="spread")
    assert port.synthesized_option_fills() == {} and await port.list_positions() == []


@pytest.mark.parametrize(("plan", "picks", "limit"), [
    (PAID_SPREAD, PAID_PICKS, "1.93"), (RECEIVED_SPREAD, RECEIVED_PICKS, "-3.08"),
])
async def test_a_net_limit_that_pays_less_or_receives_more_ends_every_leg_unfilled(
    world: tuple[ClerkSqliteRepository, SyntheticBroker], plan: ActionPlan, picks: Mapping[str, Pick], limit: str,
) -> None:
    _books, port = world
    entry = worked_entry(plan, picks, units=2, now_ms=NOON)
    _bind(port, "spread", entry)
    answer = await port.submit_option_order(
        entry.order.model_copy(update={"net_limit_price": Decimal(limit)}), client_order_id="spread",
    )
    assert (answer.status, answer.filled_quantity, answer.filled_avg_price) == ("canceled", 0, None)
    assert all(leg.filled_quantity == 0 for leg in answer.legs)
    assert await port.list_positions() == []


@pytest.mark.parametrize("leg_count", [2, 3, 4])
@pytest.mark.parametrize("phase", ["append_prefix", "before_commit", "after_commit"])
async def test_a_disk_write_failure_keeps_none_or_all_legs_and_retry_recovers_the_whole_order(
    world, tmp_path, monkeypatch, leg_count, phase,
) -> None:
    import builtins

    from app.utils import atomic_file

    _books, port = world
    legs = (plan_leg("call"), plan_leg("put", right="put"), plan_leg("other_call"), plan_leg("other_put", right="put"))[:leg_count]
    picks = {"call": (call(585), "3.10", "3.14"), "put": (put(585), "2.82", "2.86"),
             "other_call": (call(590), "1.20", "1.24"), "other_put": (put(590), "1.16", "1.20")}
    entry = worked_entry(plan(*legs), {item["leg_id"]: picks[item["leg_id"]] for item in legs}, units=2, now_ms=NOON)
    _bind(port, "disk-failure", entry)
    real_open = builtins.open
    real_replace = atomic_file.os.replace

    class FailedAppend:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def write(self, text):
            self.handle.write("".join(text.splitlines(keepends=True)[:-1]))
            self.handle.flush()
            raise OSError("disk write stopped between legs")

    def fails_between_lines(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        return FailedAppend(handle) if Path(path) == port._ledger.path and mode == "a" else handle

    def fails_at_commit(source, destination):
        if phase == "after_commit":
            real_replace(source, destination)
        raise OSError("disk write stopped at commit")

    with monkeypatch.context() as fault:
        if phase == "append_prefix":
            fault.setattr(builtins, "open", fails_between_lines)
        else:
            fault.setattr(atomic_file.os, "replace", fails_at_commit)
        try:
            await port.submit_option_order(entry.order, client_order_id="disk-failure")
        except OSError as error:
            assert "disk write stopped" in str(error)
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:several-legs")
    try:
        restarted = SyntheticBroker(account_id="sim:several-legs", source_bars=bars, clock=lambda: NOON)
        positions = await restarted.list_positions()
        assert len(positions) in (0, leg_count)
        if phase != "append_prefix":
            assert len(positions) == (0 if phase == "before_commit" else leg_count)
        looked_up = await restarted.get_option_order_by_client_order_id("disk-failure")
        assert looked_up is None or len(looked_up.legs) == leg_count
        _bind(restarted, "disk-failure", entry)
        recovered = await restarted.submit_option_order(entry.order, client_order_id="disk-failure")
        assert recovered.status == "filled" and len(recovered.legs) == leg_count
        assert len(await restarted.list_positions()) == leg_count
    finally:
        bars.close()
