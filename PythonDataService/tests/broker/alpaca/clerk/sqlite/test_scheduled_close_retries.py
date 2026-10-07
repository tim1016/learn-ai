"""How a scheduled close that failed is tried again, on a Paper account (#2913).

A scheduled close has a retry rule of its own while the session it was due
in is open: tried again every 30 seconds, never counted out, the bot never
given up. These tests are about what that rule is owed by. It is owed by
what is held and what the Clerk took, as the books show them, and not by
which order of the bot failed last. And what was tried under it is not
counted against the bot in a later session.

The venue is the double of ``resting_entry_harness``. Nothing here was sent
to Alpaca or recorded from it.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.exit_recovery import RecoveryResult, record_exit_recovery
from app.broker.alpaca.clerk.sqlite.exit_watchdog import (
    EpisodeAttempt,
    _scan_stale_exits,
    _StaleExit,
    evaluate_recovery_wait,
)
from app.broker.alpaca.clerk.sqlite.facts import ExitAcceptedFacts, ScheduledCloseFacts, UncertaintyRaisedFacts
from app.broker.alpaca.clerk.sqlite.scheduled_close import (
    SCHEDULED_CLOSE_RETRY_AFTER_MS,
    CloseStanding,
    _accept_while_still_owed,
    close_being_retried,
    held_contracts,
    scheduled_close_state,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import open_exit_stuck
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
    ExitNotFlatCause,
    ExitStuckCause,
)
from app.broker.contract.errors import BrokerOrderRejected
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    SYMBOL,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_scheduled_close import (
    CLOSE,
    DUE,
    OPEN,
    PASS_MS,
    _holds_one_call,
    _its_leg_is_sold,
    _notice,
    _sent_at,
    _the_close_fills,
    _two_bots_each_hold_one_call,
    _until,
)

REFUSED = BrokerOrderRejected("the venue refuses option orders", broker="alpaca")
THE_CLOSE = ScheduledCloseFacts(kind="expiry_day_close", due_ms=DUE, session_open_ms=OPEN)


async def _passes(paper: PaperOptions, count: int) -> None:  # noqa: F811
    """``count`` reconciliation passes fifteen seconds apart, each followed by a round of the keeper."""
    for _ in range(count):
        paper.wait(PASS_MS)
        await paper.pass_()
        await paper.round()


def _exits_taken(paper: PaperOptions, sid: str = "a") -> list[ExitAcceptedFacts]:  # noqa: F811
    return [
        ExitAcceptedFacts.from_facts_json(row["facts_json"])
        for row in paper.repo.custody_transitions()
        if row["transition_kind"] == "EXIT_ACCEPTED" and row["strategy_instance_id"] == sid
    ]


# ── the rule is owed by what is held, not by which order failed last ─────


async def test_the_bots_own_exit_failing_a_second_after_the_close_does_not_end_the_closes_retries(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """At 15:15:00 the Clerk's close is refused. At 15:15:01 the bot's own exit for that bar is refused too."""
    await _holds_one_call(paper)
    _until(paper, DUE)
    paper.venue.option_submit_error = REFUSED
    await paper.round()
    paper.wait(1_000)
    await paper.exit("a")

    await _passes(paper, 1)
    [soon_after] = scheduled_close_state(paper.repo, "a")
    await _passes(paper, 39)  # ten minutes in all

    # The close, the bot's exit a second later, then a try every thirty seconds, and the bot is never given up.
    sends = [sent - DUE for sent in _sent_at(paper)]
    assert sends[:2] == [0, 1_000]
    assert sends[2:] == [1_000 + SCHEDULED_CLOSE_RETRY_AFTER_MS * again for again in range(1, 21)]
    assert open_exit_stuck(paper.repo, "a") is None
    # Every try after the bot's own exit records the close it is a try of.
    assert [taken.the_scheduled_close for taken in _exits_taken(paper)] == [THE_CLOSE, None, *([THE_CLOSE] * 20)]
    # It reads as failed while it is failed, and never as a close that was not sent.
    [ten_minutes_on] = scheduled_close_state(paper.repo, "a")
    for state in (soon_after, ten_minutes_on):
        assert (state.standing, state.left_to_owner, state.taken_at_ms) == (CloseStanding.FAILED, False, DUE)
        assert "not sent" not in state.sentence and "sends it now" not in state.sentence

    # Alpaca takes orders again half an hour before the close, and the next try sells the contract.
    paper.venue.option_submit_error = None
    await _passes(paper, 2)
    await _the_close_fills(paper)

    assert paper.held() == {} and _notice(paper, reason_code=EXIT_NOT_FLAT_REASON_CODE) is None
    assert [taken["side"] for taken in paper.venue.option_orders.values()] == ["buy", "sell"]


async def test_while_the_bots_own_exit_is_working_after_the_close_failed_the_close_reads_as_failed_and_says_so(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE)
    paper.venue.option_submit_error = REFUSED
    await paper.round()
    paper.venue.option_submit_error = None
    paper.wait(1_000)
    await paper.exit("a")

    [state] = scheduled_close_state(paper.repo, "a")
    asked = len(paper.venue.option_submit_calls)

    # The Clerk's close failed and the bot's own order is at the venue: the read says both.
    assert state.standing is CloseStanding.FAILED and "Another exit" in state.sentence
    # While that order works, a minute of passes and rounds sends nothing more.
    await _passes(paper, 4)
    assert len(paper.venue.option_submit_calls) == asked and len(paper.venue.option_orders) == 2


async def test_the_retry_rule_is_read_from_the_books_for_one_bots_holding_and_its_own_session(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _two_bots_each_hold_one_call(paper)
    _until(paper, DUE)

    # Nothing was taken yet: no holding has a rule of its own.
    assert close_being_retried(paper.repo, "a", SYMBOL, DUE) is None

    [of_a] = [item for item in held_contracts(paper.repo, DUE).held if item.strategy_instance_id == "a"]
    assert _accept_while_still_owed(paper.repo, of_a) is not None

    assert close_being_retried(paper.repo, "a", SYMBOL, DUE) == THE_CLOSE
    assert close_being_retried(paper.repo, "a", SYMBOL.lower(), CLOSE - 1) == THE_CLOSE
    # Not before its session opens, and not from its close on.
    assert close_being_retried(paper.repo, "a", SYMBOL, OPEN - 1) is None
    assert close_being_retried(paper.repo, "a", SYMBOL, CLOSE) is None
    # Not for the bot beside it, whose close was not taken, and not for a contract the bot does not hold.
    assert close_being_retried(paper.repo, "b", SYMBOL, DUE) is None
    assert close_being_retried(paper.repo, "a", "SPY260918C00590000", DUE) is None


async def test_the_watchdog_carries_the_rule_as_one_fact_of_its_scan(paper: PaperOptions) -> None:  # noqa: F811
    """The bot's own exit failed in the morning, long before the close. At the due time the rule begins."""
    await _holds_one_call(paper)
    _until(paper, DUE - 5 * 60_000)
    paper.venue.option_submit_error = REFUSED
    await paper.exit("a")

    [before] = _scan_stale_exits(paper.repo)
    _until(paper, DUE)
    await paper.round()
    [during] = _scan_stale_exits(paper.repo)
    _until(paper, CLOSE)
    [after] = _scan_stale_exits(paper.repo)

    assert (before.scheduled_close, during.scheduled_close, after.scheduled_close) == (None, THE_CLOSE, None)
    # Ready again two minutes after an ordinary failure, and thirty seconds after one under the rule.
    assert before.ready_at_ms - before.episode["observed_at_ms"] == 120_000
    assert during.ready_at_ms - during.episode["observed_at_ms"] == SCHEDULED_CLOSE_RETRY_AFTER_MS


async def test_three_ordinary_tries_that_failed_before_the_close_do_not_make_the_clerk_give_up_under_its_rule(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """The bot's own exit fails six and a half minutes before the close is due, and is sent again three times."""
    await _holds_one_call(paper)
    _until(paper, DUE - 6 * 60_000 - 30_000)
    paper.venue.option_submit_error = REFUSED
    await paper.exit("a")
    await _passes(paper, 26)
    assert paper.clock() == DUE
    # The exit and its three ordinary tries, two minutes apart. The count is full, and nothing is given up yet.
    assert [sent - DUE for sent in _sent_at(paper)] == [-390_000, -270_000, -150_000, -30_000, 0]
    assert open_exit_stuck(paper.repo, "a") is None

    await _passes(paper, 20)

    # The close was taken at its time, and from then on its own rule holds: a try every thirty seconds.
    since_the_close = [sent - DUE for sent in _sent_at(paper) if sent >= DUE]
    assert since_the_close == [SCHEDULED_CLOSE_RETRY_AFTER_MS * again for again in range(11)]
    assert open_exit_stuck(paper.repo, "a") is None


# ── what was tried under the rule is not counted against the bot ─────────


def _an_episode_with(attempts: list[EpisodeAttempt], *, scheduled_close: ScheduledCloseFacts | None) -> _StaleExit:
    """A stuck exit of a held call, long ready to be sent again, with these orders behind it."""
    return _StaleExit(
        "a", {"uncertainty_id": "u-1", "observed_at_ms": OPEN}, ExitNotFlatCause(symbol=SYMBOL, attributed_qty=1.0),
        1.0, len(attempts), "token", tuple(attempts), OPEN, False, None, OPEN, scheduled_close=scheduled_close,
    )


def _a_failed_try(name: str, *, under: ScheduledCloseFacts | None = None) -> EpisodeAttempt:
    return EpisodeAttempt(name, True, True, False, None, True, regular_session_only=True, scheduled_close=under)


def test_tries_made_under_a_scheduled_closes_own_rule_do_not_count_toward_giving_the_bot_up() -> None:
    a_session_of_tries = [_a_failed_try(f"under-the-rule-{number}", under=THE_CLOSE) for number in range(20)]

    def judged(attempts: list[EpisodeAttempt], *, rule: ScheduledCloseFacts | None) -> RecoveryResult | None:
        return evaluate_recovery_wait(
            _an_episode_with(attempts, scheduled_close=rule), now_ms=DUE, own_exit_working=False,
            policy=ProgramLegPolicy.regular_only(), liveness=None,
        )

    # With the rule no longer standing, twenty tries made under it leave the ordinary count at zero.
    assert judged(a_session_of_tries, rule=None) is None
    assert judged([*a_session_of_tries, _a_failed_try("one"), _a_failed_try("two")], rule=None) is None
    # Three ordinary tries are counted out, as they always were.
    counted_out = judged([*a_session_of_tries, *(_a_failed_try(name) for name in ("one", "two", "three"))], rule=None)
    assert counted_out is not None and counted_out.reason_code == "EXIT_REDRIVES_EXHAUSTED"
    # While the rule stands even those are not: the session's end is the limit.
    assert judged([_a_failed_try(name) for name in ("one", "two", "three")], rule=THE_CLOSE) is None


async def test_time_a_close_fails_for_under_its_own_rule_is_not_time_toward_giving_the_bot_up(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE)
    paper.venue.option_submit_error = REFUSED
    await paper.round()
    episode = _notice(paper, reason_code=EXIT_NOT_FLAT_REASON_CODE)
    assert episode is not None
    failed = RecoveryResult("failure", "EXIT_BROKER_POSITION_MISMATCH", "The broker position differs.")

    def a_pass_fails(*, spends: bool) -> int:
        started = paper.clock()
        observed = record_exit_recovery(
            paper.repo, strategy_instance_id="a", uncertainty_id=episode["uncertainty_id"], result=failed,
            evaluated_at_ms=started, pass_started_at_ms=started, spends_failure_time=spends,
        )
        assert observed is not None
        paper.wait(PASS_MS)
        return observed.failure_elapsed_ms

    # Four passes that fail under the rule: recorded as failures, and no time is counted.
    assert [a_pass_fails(spends=False) for _ in range(4)] == [0, 0, 0, 0]
    # The same passes under the ordinary rule count the time between them, as they always did.
    assert [a_pass_fails(spends=True) for _ in range(3)] == [PASS_MS, 2 * PASS_MS, 3 * PASS_MS]


# ── a holding with a sold leg, into the next session ─────────────────────

DAY_ONE, DAY_TWO = date(2026, 9, 9), date(2026, 9, 10)
DAY_ONE_DUE = et_minute_of_day_ms(DAY_ONE, 15 * 60 + 50)
DAY_ONE_CLOSE = et_minute_of_day_ms(DAY_ONE, 16 * 60)
DAY_TWO_OPEN = et_minute_of_day_ms(DAY_TWO, 9 * 60 + 30)
DAY_TWO_DUE = et_minute_of_day_ms(DAY_TWO, 15 * 60 + 50)


async def _a_session_of_refused_closes(paper: PaperOptions, monkeypatch: pytest.MonkeyPatch) -> int:  # noqa: F811
    """The sold leg's close is refused from 15:50 to the close of day one. How many orders were tried."""
    await _holds_one_call(paper)
    _its_leg_is_sold(paper, monkeypatch)
    _until(paper, DAY_ONE_DUE)
    paper.venue.option_submit_error = REFUSED
    await paper.round()
    while paper.clock() + PASS_MS < DAY_ONE_CLOSE:
        paper.wait(PASS_MS)
        await paper.pass_()
    tried = len(_sent_at(paper))
    # Twenty tries in the ten minutes, the bot not given up, and the close recorded as missed when the session ends.
    assert tried == 20 and open_exit_stuck(paper.repo, "a") is None
    _until(paper, DAY_ONE_CLOSE)
    await paper.pass_()
    await paper.round()
    assert _notice(paper) is not None and open_exit_stuck(paper.repo, "a") is None
    return tried


async def test_after_a_session_of_refused_closes_the_close_is_sent_again_at_the_next_open(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Shown on a holding whose sold leg is written by hand: no entry with a sold leg can be made yet."""
    tried = await _a_session_of_refused_closes(paper, monkeypatch)
    paper.venue.option_submit_error = None

    _until(paper, DAY_TWO_OPEN + 1_000)
    await paper.pass_()

    # The first pass after the open sends it, and the Clerk has not stopped exiting the bot.
    assert len(_sent_at(paper)) == tried + 1 and open_exit_stuck(paper.repo, "a") is None
    await _the_close_fills(paper)
    await paper.round()
    assert paper.held() == {} and _notice(paper) is None


async def test_a_bot_still_refused_the_next_session_is_given_up_after_three_ordinary_tries_and_told_every_try(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    tried = await _a_session_of_refused_closes(paper, monkeypatch)

    _until(paper, DAY_TWO_OPEN + 1_000)
    await paper.pass_()
    await _passes(paper, 25)

    # Three tries two minutes apart from the open, and then the Clerk stops exiting the bot by itself.
    assert [sent - (DAY_TWO_OPEN + 1_000) for sent in _sent_at(paper)[tried:]] == [0, 120_000, 240_000]
    stuck = open_exit_stuck(paper.repo, "a")
    assert stuck is not None
    # The notice counts every re-drive that was sent: nineteen under the close's own rule, and these three.
    assert ExitStuckCause.from_mapping(
        UncertaintyRaisedFacts.from_facts_json(stuck["facts_json"]).cause_facts
    ).redrive_count == tried - 1 + 3

    # That afternoon's close is left to the owner: nothing is taken and nothing is sent.
    _until(paper, DAY_TWO_DUE)
    afternoon = await paper.round()
    assert afternoon.closes.taken == () and len(_sent_at(paper)) == tried + 3
    [left] = scheduled_close_state(paper.repo, "a")
    assert left.left_to_owner


async def test_time_failed_under_the_rule_is_not_carried_into_the_next_session(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """After the first refusal Alpaca's account shows no such position, so every pass refuses to send again."""
    await _holds_one_call(paper)
    _its_leg_is_sold(paper, monkeypatch)
    _until(paper, DAY_ONE_DUE)
    paper.venue.option_submit_error = REFUSED
    await paper.round()

    async def no_positions() -> list[object]:
        return []

    monkeypatch.setattr(paper.account, "list_positions", no_positions)
    while paper.clock() + PASS_MS < DAY_ONE_CLOSE:
        paper.wait(PASS_MS)
        await paper.pass_()
    # Nine minutes of passes that each failed, with nothing sent after the first try. The bot is not given up.
    assert len(_sent_at(paper)) == 1 and open_exit_stuck(paper.repo, "a") is None

    _until(paper, DAY_TWO_OPEN + 1_000)
    await paper.pass_()
    paper.wait(PASS_MS)
    await paper.pass_()

    # The next morning's first failures start the ordinary clock from nothing.
    assert len(_sent_at(paper)) == 1 and open_exit_stuck(paper.repo, "a") is None


async def test_a_new_session_counts_its_tries_from_zero_and_owes_a_close_of_its_own(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    tried = await _a_session_of_refused_closes(paper, monkeypatch)

    # Still refused the next morning: the ordinary rule, counted from nothing. Three tries, two minutes apart.
    _until(paper, DAY_TWO_OPEN + 1_000)
    await paper.pass_()
    await _passes(paper, 15)
    in_the_morning = [sent - (DAY_TWO_OPEN + 1_000) for sent in _sent_at(paper)[tried:]]
    assert in_the_morning == [0, 120_000] and open_exit_stuck(paper.repo, "a") is None

    # Alpaca takes the third, and it rests unfilled all day. That afternoon's close is its own decision.
    paper.venue.option_submit_error = None
    await _passes(paper, 2)
    assert len(_sent_at(paper)) == tried + 3
    _until(paper, DAY_TWO_DUE)
    afternoon = await paper.round()

    # An exit of the bot is working, so no second closing order is made: the day's close reads as another exit's.
    assert afternoon.closes.taken == () and len(_sent_at(paper)) == tried + 3
    [of_day_two] = held_contracts(paper.repo, paper.clock()).held
    assert of_day_two.owed is not None and of_day_two.owed.due_ms == DAY_TWO_DUE
    assert of_day_two.holding is not None and of_day_two.holding.state == "other_exit"
