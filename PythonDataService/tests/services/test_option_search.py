"""Where an option search runs: a worker process of its own, the answer it brings back, and each way it brings none.

The tests that start a real worker process say so in their names. Every
other test hands the lane an executor that runs the search here, through the
same pickling a worker's pipe does, or one whose answers the test holds back.
"""

from __future__ import annotations

import ast
import asyncio
import os
import pickle
import signal
from collections.abc import Callable
from concurrent.futures import Executor, Future
from concurrent.futures.process import BrokenProcessPool
from dataclasses import fields, replace
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from app.options.american import DEFAULT_TREE_STEPS
from app.options.named_structures import NamedStructure
from app.options.quotes import QuoteSide
from app.options.resolver import ContractsNotResolved, FeedState, Listing, Resolution, ResolverRefusal
from app.options.structure_catalogue import starting_plan
from app.schemas.action_plan import ActionPlan
from app.services import option_search
from app.services.option_search import (
    Found,
    OptionSearches,
    OptionSearchLane,
    PreviewSearches,
    SearchAsk,
    SearchNotRun,
    SearchNotRunReason,
)

# The resolver's own scenes: SPY at 585 on a dollar-wide ladder, selected on a Tuesday at 10:31:02.
from tests.options import test_resolver as scenes


class InThisProcess(Executor):
    """Runs what it is handed at once, here, through the pickling a worker process's pipe does."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        answered: Future[Any] = Future()
        try:
            called, with_args, with_kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            answered.set_result(pickle.loads(pickle.dumps(called(*with_args, **with_kwargs))))
        except Exception as failed:
            answered.set_exception(failed)
        return answered


def searches_in_this_process() -> PreviewSearches[Any]:
    """A preview lane that searches here: what a test of something else hands in, to stay quick."""
    return PreviewSearches(OptionSearchLane("preview", executor=InThisProcess))


class Held(Executor):
    """A worker whose searches answer only when the test lets them. The first call, which starts it, answers at once."""

    def __init__(self) -> None:
        self.searches: list[Future[Any]] = []
        self.shut_down = False

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        answer: Future[Any] = Future()
        if fn is option_search.search:
            # A worker runs one search at a time: this one is running from now.
            answer.set_running_or_notify_cancel()
            answer.asked = args[0]  # type: ignore[attr-defined]
            self.searches.append(answer)
        else:
            answer.set_result(fn(*args, **kwargs))
        return answer

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shut_down = True

    def let_go(self, at: int = 0) -> None:
        """Let one held search run to its answer."""
        answer = self.searches[at]
        answer.set_result(option_search.search(answer.asked))  # type: ignore[attr-defined]


class Workers:
    """Hands a lane one ``Held`` worker after another, and keeps them."""

    def __init__(self) -> None:
        self.made: list[Held] = []

    def __call__(self) -> Held:
        self.made.append(Held())
        return self.made[-1]


def _ask(plan: ActionPlan, scene: scenes._Scene | None = None) -> SearchAsk:
    scene = scene or scenes._scene()
    return SearchAsk(
        plan=plan,
        chain=scene.chain,
        underlying=scene.underlying,
        feed=scene.feed,
        model=scene.model,
        limits=scene.limits,
        decision_bar_close_ms=scene.decision_bar_close_ms,
        selection_ms=scene.selection_ms,
    )


_AT_THE_MONEY = scenes._one("call", scenes._AT_THE_MONEY)
_SPREAD = scenes._plan(
    scenes._leg("long_call", "call", scenes._AT_THE_MONEY),
    scenes._leg("short_call", "call", {"selector": "leg_distance", "leg_id": "long_call", "dollars": 5.0}, position="short"),
)


def _as_made(refused: ContractsNotResolved) -> tuple[object, ...]:
    return (refused.reason, refused.leg_ids, str(refused), refused.fault, refused.contract, refused.evidence, refused.recheck)


async def _moments(turns: int = 5) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


# ---- What crosses to the worker, and what comes back -------------------------------


def test_everything_a_search_is_handed_reaches_the_worker_equal_to_what_was_sent() -> None:
    """The supervisor hands the chain's watched contracts over as a read-only view, which does not pickle as it is."""
    scene = scenes._scene()
    as_the_supervisor_hands_it = replace(scene, chain=replace(scene.chain, watched=MappingProxyType(dict(scene.chain.watched))))
    asked = _ask(_SPREAD, as_the_supervisor_hands_it)

    arrived = pickle.loads(pickle.dumps(asked.as_plain_data()))

    with pytest.raises(TypeError):
        pickle.dumps(asked)
    for part in fields(SearchAsk):
        assert getattr(arrived, part.name) == getattr(asked, part.name), part.name
    assert arrived == asked


async def test_a_resolution_comes_back_from_the_worker_as_the_resolver_made_it() -> None:
    scene = scenes._scene()
    lane = OptionSearchLane("entry", executor=InThisProcess)

    found = await lane.resolve(
        _SPREAD,
        chain=scene.chain,
        underlying=scene.underlying,
        feed=scene.feed,
        model=scene.model,
        limits=scene.limits,
        decision_bar_close_ms=scene.decision_bar_close_ms,
        selection_ms=scene.selection_ms,
    )

    here = scene.resolve(_SPREAD)
    assert isinstance(found, Resolution) and found == here
    # In the plan's order, and read-only, as the resolver hands them.
    assert list(found.picked) == list(here.picked) == ["long_call", "short_call"]
    assert all(isinstance(part, MappingProxyType) for part in (found.picked, found.quotes, found.multipliers))
    assert found.evidence == here.evidence


def _quoted_at(scene: scenes._Scene, strike: int, ago_ms: int) -> scenes._Scene:
    contract = scenes._contract("call", strike)
    quote = scene.quote_of(contract)
    when = scene.selection_ms - ago_ms
    return scene.holding(contract, replace(quote, bid=QuoteSide(quote.bid.price, when), ask=QuoteSide(quote.ask.price, when)))


def _crossed(scene: scenes._Scene, strike: int) -> scenes._Scene:
    contract = scenes._contract("call", strike)
    quote = scene.quote_of(contract)
    return scene.holding(contract, replace(quote, bid=QuoteSide(quote.ask.price + 1, quote.bid.received_ms)))


def _lists_good_friday() -> scenes._Scene:
    scene = scenes._scene(day=date(2026, 3, 30), expiries=(date(2026, 4, 10),))
    on_no_session = Listing(
        scenes._contract("call", 585, date(2026, 4, 3)), multiplier=100, shares_delivered=100, exercise="american"
    )
    return replace(scene, chain=replace(scene.chain, listings=(*scene.chain.listings, on_no_session)))


_REFUSALS: dict[ResolverRefusal, tuple[Callable[[], scenes._Scene], ActionPlan]] = {
    ResolverRefusal.FEED_UNHEALTHY: (
        lambda: replace(scenes._scene(), feed=FeedState(generation=scenes._GENERATION, unhealthy="reconnecting")),
        _AT_THE_MONEY,
    ),
    ResolverRefusal.UNDERLYING_DID_NOT_TRADE: (
        lambda: replace(scenes._scene(), underlying=replace(scenes._scene().underlying, pricing_bar_close=None)),
        _AT_THE_MONEY,
    ),
    ResolverRefusal.UNDERLYING_QUOTE_UNUSABLE: (
        lambda: replace(scenes._scene(), underlying=replace(scenes._scene().underlying, quote=None)),
        _AT_THE_MONEY,
    ),
    ResolverRefusal.EXPIRY_NOT_LISTED: (
        scenes._scene,
        scenes._one("call", scenes._AT_THE_MONEY, expiry={"selector": "min_dte", "days": 90}),
    ),
    ResolverRefusal.EXPIRY_NOT_A_SESSION: (
        _lists_good_friday,
        scenes._one("call", {"selector": "delta", "target": 0.3}, expiry={"selector": "min_dte", "days": 1}),
    ),
    ResolverRefusal.PAST_ENTRY_CUTOFF: (
        lambda: scenes._scene(selected_at=(15, 0, 0)),
        scenes._one("call", scenes._AT_THE_MONEY, expiry={"selector": "same_day"}),
    ),
    ResolverRefusal.STRIKE_NOT_LISTED: (scenes._scene, scenes._one("call", {"selector": "absolute", "strike": 700.0})),
    # Listed and not watched: nothing is known of its quote.
    ResolverRefusal.SEARCH_INCOMPLETE: (scenes._scene, scenes._one("call", {"selector": "absolute", "strike": 565.0})),
    ResolverRefusal.NO_USABLE_QUOTE: (lambda: _crossed(scenes._scene(), 585), _AT_THE_MONEY),
    ResolverRefusal.NOTHING_CLOSE_ENOUGH: (scenes._scene, scenes._one("call", {"selector": "percent", "percent": 50.0})),
    ResolverRefusal.OBSERVATIONS_APART: (lambda: _quoted_at(scenes._scene(), 590, 2_401), _SPREAD),
}


def test_every_refusal_a_selection_can_end_in_has_its_case_here() -> None:
    """A reconnect between the selection and the recheck is the recheck's refusal, which searches for nothing."""
    assert set(_REFUSALS) == set(ResolverRefusal) - {ResolverRefusal.FEED_RECONNECTED}


@pytest.mark.parametrize("reason", list(_REFUSALS), ids=lambda reason: reason.name.lower())
async def test_each_refusal_comes_back_from_the_worker_as_the_resolver_made_it(reason: ResolverRefusal) -> None:
    make, plan = _REFUSALS[reason]
    scene = make()
    here = scene.refusal(plan)
    lane = OptionSearchLane("entry", executor=InThisProcess)

    found = await lane.search(_ask(plan, scene))

    assert here.reason is reason
    assert isinstance(found, ContractsNotResolved) and _as_made(found) == _as_made(here)
    # Through the lane's own ``resolve`` it is raised, as the resolver raises it.
    with pytest.raises(ContractsNotResolved) as raised:
        await lane.resolve(plan, **{part.name: getattr(_ask(plan, scene), part.name) for part in fields(SearchAsk)[1:]})
    assert _as_made(raised.value) == _as_made(here)


async def test_what_could_not_describe_a_market_is_the_callers_mistake_from_the_worker_too() -> None:
    """A selection before the close of the bar it was decided on. The resolver raises ``ValueError``, never a refusal."""
    scene = scenes._scene()
    backwards = replace(_ask(_AT_THE_MONEY, scene), selection_ms=scene.decision_bar_close_ms - 1)
    with pytest.raises(ValueError) as here:
        scenes.resolve_contracts(
            backwards.plan, **{part.name: getattr(backwards, part.name) for part in fields(SearchAsk)[1:]}
        )

    with pytest.raises(ValueError) as from_the_worker:
        await OptionSearchLane("entry", executor=InThisProcess).search(backwards)

    assert not isinstance(from_the_worker.value, ContractsNotResolved)
    assert str(from_the_worker.value) == str(here.value)


# ---- A real worker process ------------------------------------------------------------


async def test_a_real_worker_process_answers_as_the_resolver_does_here_for_every_starting_plan() -> None:
    """The twelve named structures as the builder starts them, each with a leg picked by delta."""
    scene = scenes._scene()
    lane = OptionSearchLane("preview")
    try:
        for name in NamedStructure:
            plan = starting_plan(name, "SPY")

            found = await lane.search(_ask(plan, scene))

            assert isinstance(found, Resolution), (name, found)
            assert found == scene.resolve(plan), name
        assert lane.worker_pid not in (None, os.getpid())
    finally:
        lane.close()


async def test_a_real_worker_that_dies_mid_search_is_a_search_not_run_and_the_next_search_starts_another() -> None:
    # The default tree, so that the search is still running when its worker is killed.
    slow = scenes._scene(model=replace(scenes._MODEL, tree_steps=DEFAULT_TREE_STEPS))
    by_delta = starting_plan(NamedStructure.IRON_CONDOR, "SPY")
    lane = OptionSearchLane("entry")
    try:
        assert isinstance(await lane.search(_ask(_AT_THE_MONEY)), Resolution)
        first_worker = lane.worker_pid
        searching = lane.start(_ask(by_delta, slow))
        await asyncio.sleep(0.1)
        os.kill(first_worker, signal.SIGKILL)

        lost = await searching
        again = await lane.search(_ask(_AT_THE_MONEY))

        assert isinstance(lost, SearchNotRun) and lost.reason is SearchNotRunReason.WORKER_FAILED
        assert isinstance(again, Resolution) and again == scenes._scene().resolve(_AT_THE_MONEY)
        assert lane.worker_pid not in (None, first_worker, os.getpid())
    finally:
        lane.close()


# ---- A worker that dies, hangs, or cannot be started ------------------------------------


class _Dies(Held):
    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        answer = super().submit(fn, *args, **kwargs)
        if fn is option_search.search:
            answer.set_exception(BrokenProcessPool("the worker was terminated abruptly"))
        return answer


async def test_a_worker_that_dies_is_a_search_not_run_never_a_refusal_and_is_replaced() -> None:
    made: list[Executor] = []

    def worker() -> Executor:
        made.append(_Dies() if not made else InThisProcess())
        return made[-1]

    lane = OptionSearchLane("entry", executor=worker)

    lost = await lane.search(_ask(_AT_THE_MONEY))
    again = await lane.search(_ask(_AT_THE_MONEY))

    assert isinstance(lost, SearchNotRun) and not isinstance(lost, ContractsNotResolved)
    assert lost.reason is SearchNotRunReason.WORKER_FAILED and lost.detail
    assert made[0].shut_down and len(made) == 2
    assert isinstance(again, Resolution)


async def test_a_worker_that_does_not_answer_within_the_bound_is_ended_and_replaced() -> None:
    workers = Workers()
    lane = OptionSearchLane("entry", executor=workers, hang_bound_s=0.05)

    hung = await lane.search(_ask(_AT_THE_MONEY))
    searching_again = lane.start(_ask(_AT_THE_MONEY))
    await _moments()
    workers.made[1].let_go()

    assert isinstance(hung, SearchNotRun) and hung.reason is SearchNotRunReason.WORKER_DID_NOT_ANSWER
    assert workers.made[0].shut_down and len(workers.made) == 2
    assert isinstance(await searching_again, Resolution)


@pytest.mark.parametrize("failure", [PermissionError("no semaphores to be had"), NotImplementedError("no process pools here")])
async def test_a_worker_that_cannot_be_started_is_a_search_not_run(failure: Exception) -> None:
    def worker() -> Executor:
        raise failure

    lane = OptionSearchLane("entry", executor=worker)
    lane.warm()

    lost = await lane.search(_ask(_AT_THE_MONEY))

    assert isinstance(lost, SearchNotRun) and lost.reason is SearchNotRunReason.WORKER_FAILED


async def test_a_search_that_raises_in_the_worker_is_not_run_and_is_not_an_answer() -> None:
    class Faulty(Held):
        def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
            answer = super().submit(fn, *args, **kwargs)
            if fn is option_search.search:
                answer.set_exception(KeyError("a fault in the search itself"))
            return answer

    lost = await OptionSearchLane("entry", executor=Faulty).search(_ask(_AT_THE_MONEY))

    assert isinstance(lost, SearchNotRun) and lost.reason is SearchNotRunReason.FAILED


def test_a_process_that_never_searches_never_starts_a_worker() -> None:
    made: list[Executor] = []

    def worker() -> Executor:
        made.append(InThisProcess())
        return made[-1]

    searches = OptionSearches(executor=worker)
    searches.close()

    assert made == []


async def test_closing_a_lane_stops_its_worker_and_does_not_wait_for_a_search_in_hand() -> None:
    workers = Workers()
    lane = OptionSearchLane("entry", executor=workers)
    searching = lane.start(_ask(_AT_THE_MONEY))
    await _moments()

    lane.close()

    with pytest.raises(asyncio.CancelledError):
        await searching
    assert workers.made[0].shut_down


# ---- One search at a time, and the two lanes apart ---------------------------------------


async def test_a_lane_runs_one_search_at_a_time_in_the_order_asked() -> None:
    workers = Workers()
    lane = OptionSearchLane("entry", executor=workers)

    first, second = lane.start(_ask(_AT_THE_MONEY)), lane.start(_ask(_SPREAD))
    await _moments()
    (worker,) = workers.made
    handed_at_first = [answer.asked.plan for answer in worker.searches]
    worker.let_go(0)
    await first
    await _moments()
    handed_after_the_first = [answer.asked.plan for answer in worker.searches]
    worker.let_go(1)

    assert (handed_at_first, handed_after_the_first) == ([_AT_THE_MONEY], [_AT_THE_MONEY, _SPREAD])
    assert (await first).picked.keys() == {"only"} and (await second).picked.keys() == {"long_call", "short_call"}


async def test_an_entrys_search_is_not_delayed_by_a_previews_search_that_is_still_running() -> None:
    preview_workers = Workers()
    entry = OptionSearchLane("entry", executor=InThisProcess)
    previews: PreviewSearches[str] = PreviewSearches(OptionSearchLane("preview", executor=preview_workers))

    a_page = asyncio.ensure_future(previews.search("page-1", _ask(_SPREAD), standing_on="what the page built"))
    await _moments()
    for_a_bot = await asyncio.wait_for(entry.search(_ask(_AT_THE_MONEY)), timeout=5)

    assert isinstance(for_a_bot, Resolution)
    assert not a_page.done()
    preview_workers.made[0].let_go()
    assert isinstance(await a_page, Found)


def test_the_two_lanes_of_a_process_are_two_workers() -> None:
    made: list[Executor] = []

    def worker() -> Executor:
        made.append(InThisProcess())
        return made[-1]

    searches = OptionSearches(executor=worker)
    searches.entry.warm()
    searches.preview.warm()

    assert len(made) == 2 and made[0] is not made[1]
    assert searches.entry is not searches.preview.lane


# ---- The preview lane's rules ---------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _previews(workers: Workers, *, wait_s: float = 5.0, clock: _Clock | None = None) -> PreviewSearches[str]:
    return PreviewSearches(
        OptionSearchLane("preview", executor=workers), wait_s=wait_s, kept_for_s=5.0, clock=clock or _Clock()
    )


async def test_two_asks_under_one_key_start_one_search_and_the_second_is_told_it_is_running() -> None:
    workers = Workers()
    previews = _previews(workers)

    first = asyncio.ensure_future(previews.search("page-1", _ask(_SPREAD), standing_on="a spread"))
    await _moments()
    second = await previews.search("page-1", _ask(_SPREAD), standing_on="a spread")
    (worker,) = workers.made
    searches_begun = len(worker.searches)
    worker.let_go()
    found = await first

    assert isinstance(second, SearchNotRun) and second.reason is SearchNotRunReason.ALREADY_RUNNING
    assert second.reason.still_running
    assert searches_begun == 1 == len(worker.searches)
    assert isinstance(found, Found) and found.standing_on == "a spread"
    assert found.outcome == scenes._scene().resolve(_SPREAD)


async def test_one_search_runs_and_one_waits_and_a_third_page_is_told_the_lane_is_busy() -> None:
    workers = Workers()
    previews = _previews(workers)

    running = asyncio.ensure_future(previews.search("page-1", _ask(_SPREAD), standing_on="one"))
    waiting = asyncio.ensure_future(previews.search("page-2", _ask(_AT_THE_MONEY), standing_on="two"))
    await _moments()
    third = await previews.search("page-3", _ask(_AT_THE_MONEY), standing_on="three")
    (worker,) = workers.made
    worker.let_go(0)
    await running
    await _moments()
    worker.let_go(1)

    assert isinstance(third, SearchNotRun) and third.reason is SearchNotRunReason.LANE_BUSY
    assert isinstance(await waiting, Found)
    # With the lane free again the third page's search is begun.
    after = asyncio.ensure_future(previews.search("page-3", _ask(_AT_THE_MONEY), standing_on="three"))
    await _moments()
    worker.let_go(2)
    assert isinstance(await after, Found)


async def test_a_search_that_outlives_its_ask_is_kept_for_the_pages_next_ask_that_stands_on_the_same_thing() -> None:
    workers = Workers()
    previews = _previews(workers, wait_s=0.05)

    told = await previews.search("page-1", _ask(_SPREAD), standing_on="a spread")
    while_it_runs = await previews.search("page-1", _ask(_SPREAD), standing_on="a spread")
    (worker,) = workers.made
    worker.let_go()
    await _moments()
    next_ask = await previews.search("page-1", _ask(_SPREAD), standing_on="a spread")

    assert isinstance(told, SearchNotRun) and told.reason is SearchNotRunReason.NOT_DONE_IN_TIME
    assert isinstance(while_it_runs, SearchNotRun) and while_it_runs.reason is SearchNotRunReason.ALREADY_RUNNING
    assert isinstance(next_ask, Found) and next_ask.outcome == scenes._scene().resolve(_SPREAD)
    # The one search answered both: nothing was searched for again.
    assert len(worker.searches) == 1


async def test_a_kept_answer_is_dropped_when_the_page_has_built_something_else() -> None:
    workers = Workers()
    previews = _previews(workers, wait_s=0.05)

    await previews.search("page-1", _ask(_SPREAD), standing_on="a spread")
    (worker,) = workers.made
    worker.let_go(0)
    await _moments()
    changed = asyncio.ensure_future(previews.search("page-1", _ask(_AT_THE_MONEY), standing_on="one call"))
    await _moments()
    worker.let_go(1)
    found = await changed

    assert isinstance(found, Found) and found.standing_on == "one call"
    assert found.outcome.picked.keys() == {"only"}


async def test_a_kept_answer_is_handed_over_once_and_only_for_a_few_seconds() -> None:
    workers, clock = Workers(), _Clock()
    previews = _previews(workers, wait_s=0.05, clock=clock)

    async def outlived(page: str) -> None:
        await previews.search(page, _ask(_SPREAD), standing_on="a spread")
        workers.made[0].let_go(len(workers.made[0].searches) - 1)
        await _moments()

    await outlived("page-1")
    clock.now += 5.0
    in_time = await previews.search("page-1", _ask(_SPREAD), standing_on="a spread")
    await outlived("page-2")
    clock.now += 5.001
    too_late = asyncio.ensure_future(previews.search("page-2", _ask(_SPREAD), standing_on="a spread"))
    await _moments()

    assert isinstance(in_time, Found)
    # Too old to hand over: a search is begun afresh.
    assert not too_late.done() and len(workers.made[0].searches) == 3
    workers.made[0].let_go(2)
    assert isinstance(await too_late, Found)


async def test_a_worker_lost_to_a_preview_is_told_as_not_run_and_nothing_is_kept_of_it() -> None:
    made: list[Executor] = []

    def worker() -> Executor:
        made.append(_Dies() if not made else InThisProcess())
        return made[-1]

    previews: PreviewSearches[str] = PreviewSearches(OptionSearchLane("preview", executor=worker))

    lost = await previews.search("page-1", _ask(_SPREAD), standing_on="a spread")
    again = await previews.search("page-1", _ask(_SPREAD), standing_on="a spread")

    assert isinstance(lost, SearchNotRun) and lost.reason is SearchNotRunReason.WORKER_FAILED
    assert not lost.reason.still_running
    assert isinstance(again, Found)


# ---- Nothing else searches in the Clerk's own process ------------------------------------------


def test_only_the_search_worker_calls_the_resolver() -> None:
    """A search run on the event loop, or in a thread of the Clerk's process, stops the loop for as long as it runs
    (ADR 0076). Everything outside the resolver's own package reaches it through a lane."""
    app = Path(option_search.__file__).resolve().parents[1]
    callers = []
    for module in sorted(app.rglob("*.py")):
        if module.parent == app / "options":
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        named = {getattr(node, "id", None) or getattr(node, "attr", None) for node in ast.walk(tree)}
        if "resolve_contracts" in named:
            callers.append(module.relative_to(app).as_posix())

    assert callers == ["services/option_search.py"]
