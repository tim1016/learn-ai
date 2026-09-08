"""Shadow-vs-paper-twin reconciliation gates on decisions and reports prices (ADR 0059 D2)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.shadow_sessions import ShadowSessionLedger
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicProjectionUnavailable
from app.broker.alpaca.clerk.sqlite.models import RunResource
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.research.parity.qc_reconciler import DivergenceCategory
from app.schemas.signal_program_seal import SealedBotProgram
from app.services.alpaca_shadow_reconciliation import (
    FILL_PRICE_ATOL,
    ShadowTwinMismatch,
    TwinFill,
    _digest_default,
    evaluate_shadow_gate,
    reconcile_twin_day,
    twins_agree,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import et_midnight_ms

DAY = date(2026, 9, 8)
PRIOR_DAY = date(2026, 9, 4)  # the trading day before DAY (2026-09-07 is Labor Day)
OPEN = session_open_ms_utc(DAY)
CLOSE = session_close_ms_utc(DAY)
SID, TWIN = "ema-shadow-1", "ema-paper-1"


def _fill(
    minute: int,
    *,
    day: date = DAY,
    symbol: str = "SPY",
    side: str = "buy",
    qty: str = "1",
    price: str = "100.00",
    ref: str = "a",
) -> TwinFill:
    return TwinFill(
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        fill_price=Decimal(price),
        filled_at_ms=et_minute_of_day_ms(day, minute),
        order_ref=f"learn-ai/x/v1:{ref}",
    )


def _reconcile(shadow: list[TwinFill], twin: list[TwinFill]):
    return reconcile_twin_day(
        session_open_ms=OPEN,
        strategy_instance_id=SID,
        twin_strategy_instance_id=TWIN,
        shadow_fills=tuple(shadow),
        twin_fills=tuple(twin),
    )


def test_identical_days_pass_with_no_divergence_and_a_stable_digest() -> None:
    first = _reconcile(
        [_fill(600), _fill(660, side="sell")],
        [_fill(600, ref="p"), _fill(660, side="sell", ref="q")],
    )
    second = _reconcile(
        [_fill(600), _fill(660, side="sell")],
        [_fill(600, ref="p"), _fill(660, side="sell", ref="q")],
    )
    assert first.passed is True and first.divergences == ()
    assert first.report_sha256() == second.report_sha256()
    assert len(first.report_sha256()) == 64


@pytest.mark.parametrize(
    ("shadow", "twin", "category"),
    [
        ([_fill(600)], [], DivergenceCategory.DECISION_MISMATCH),
        ([], [_fill(600)], DivergenceCategory.DECISION_MISMATCH),
        (
            [_fill(600)],
            [_fill(600, symbol="QQQ")],
            DivergenceCategory.DECISION_MISMATCH,
        ),
        ([_fill(600)], [_fill(600, side="sell")], DivergenceCategory.DIRECTION_MISMATCH),
        ([_fill(600, qty="2")], [_fill(600, qty="1")], DivergenceCategory.QUANTITY_MISMATCH),
    ],
)
def test_shape_divergences_gate(
    shadow: list[TwinFill], twin: list[TwinFill], category: DivergenceCategory
) -> None:
    result = _reconcile(shadow, twin)
    assert [d.category for d in result.gating] == [category]
    assert result.passed is False


def test_price_and_time_drift_are_reported_never_gated() -> None:
    result = _reconcile([_fill(600, price="100.00")], [_fill(600, price="100.07")])
    assert [d.category for d in result.divergences] == [DivergenceCategory.FILL_PRICE_DRIFT]
    assert result.gating == () and result.passed is True
    assert result.max_fill_price_drift == Decimal("0.07")
    assert result.max_fill_time_drift_ms == 0
    assert result.fill_price_atol == FILL_PRICE_ATOL == Decimal("0.01")
    within = _reconcile([_fill(600, price="100.00")], [_fill(600, price="100.01")])
    assert within.divergences == () and within.max_fill_price_drift == Decimal("0.01")

    # Positional pairing is blind to *when* each decision was made: three
    # minutes apart still pairs, still passes, and the distance is reported.
    apart = _reconcile([_fill(600)], [_fill(603, ref="p")])
    assert apart.divergences == () and apart.passed is True
    assert apart.max_fill_time_drift_ms == et_minute_of_day_ms(DAY, 603) - et_minute_of_day_ms(
        DAY, 600
    )
    assert _reconcile([_fill(600)], []).max_fill_time_drift_ms is None


def test_the_digest_encoder_names_every_type_it_will_hash() -> None:
    assert _digest_default(Decimal("1.0")) == "1.0"
    assert _digest_default(DivergenceCategory.DECISION_MISMATCH) == "decision_mismatch"
    with pytest.raises(TypeError, match="no digest representation"):
        _digest_default(object())


def _seal(**overrides: object) -> SealedBotProgram:
    base = dict(
        configured_signal_hash="a" * 64,
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        carryover_policy="FORBID",
        mode="trade",
        sealed_account_id="PA-TEST",
    )
    return SealedBotProgram.model_construct(**{**base, **overrides})


def test_twins_agree_on_the_configured_signal_and_plan_only() -> None:
    shadow = _seal(sealed_account_id="shadow:9LIVE0001")
    assert twins_agree(shadow, _seal()) is None
    assert twins_agree(shadow, _seal(configured_signal_hash="b" * 64)) is not None
    assert twins_agree(shadow, _seal(quantity=2)) is not None
    assert twins_agree(shadow, _seal(mode="dry_run")) is not None
    assert twins_agree(_seal(), _seal()) is not None  # the shadow side must be shadow-sealed
    assert "not the paper account" in str(
        twins_agree(shadow, _seal(sealed_account_id="shadow:9LIVE0002"))
    )
    assert "action plan" in str(twins_agree(shadow, _seal(action_plan=alpaca_v1_action_plan("QQQ"))))
    assert "carryover policy" in str(twins_agree(shadow, _seal(carryover_policy="ALLOW")))


UNREADABLE = "SQLite account fill evidence is incomplete for this window: a filled external order"


class _Source:
    """One world's fill reader, which carries exactly one instance.

    Two of these, never one double serving both sides. A single shared
    double answers identically whichever authority ``_judge_day`` asks, so a
    seam that read the twin's fills from the shadow authority would still
    pass every test here. In production the shadow custody database does not
    carry the paper twin's instance at all, so the same mistake fails closed
    on ``not_evaluable``; ``label``/``instance_id`` reproduce that, and
    ``consulted`` records who was actually asked.
    """

    def __init__(
        self,
        label: str,
        instance_id: str,
        fills: Sequence[TwinFill] = (),
        runs: Sequence[RunResource] = (),
        *,
        unreadable: Sequence[date] = (),
    ) -> None:
        self.label = label
        self.consulted: list[str] = []
        self._instance_id = instance_id
        self._fills, self._runs = tuple(fills), tuple(runs)
        self._unreadable = frozenset(et_midnight_ms(day) for day in unreadable)

    def fills_between(
        self, *, strategy_instance_id: str, from_ms: int, to_ms: int
    ) -> tuple[TwinFill, ...]:
        self.consulted.append(strategy_instance_id)
        if strategy_instance_id != self._instance_id:
            raise EconomicProjectionUnavailable(
                f"the {self.label} authority does not carry '{strategy_instance_id}'"
            )
        if from_ms in self._unreadable:
            raise EconomicProjectionUnavailable(UNREADABLE)
        # Half-open, exactly as the protocol and the SQLite projection define it.
        return tuple(f for f in self._fills if from_ms <= f.filled_at_ms < to_ms)

    def runs_for_strategy(self, strategy_instance_id: str) -> tuple[RunResource, ...]:
        if strategy_instance_id != self._instance_id:
            return ()
        return self._runs


def _run(
    started_ms: int,
    stopped_ms: int | None,
    *,
    sid: str = SID,
    run_id: str = "run-1",
) -> RunResource:
    return RunResource(
        run_id=run_id,
        strategy_instance_id=sid,
        lifecycle_run_id="l-1",
        state="ACTIVE" if stopped_ms is None else "STOPPED",
        started_at_ms=started_ms,
        stopped_at_ms=stopped_ms,
    )


def _twin_run(started_ms: int, stopped_ms: int | None) -> RunResource:
    """A run of the paper twin — a different instance, and a different run id.

    Named apart from the shadow run so a verdict's ``shadow_run_id`` cannot
    accidentally read as satisfied by the twin's run.
    """
    return _run(started_ms, stopped_ms, sid=TWIN, run_id="twin-run-1")


def _binding(sid: str, sealed: str, *, use_rth: bool = True) -> BrokerBotBinding:
    # ``model_construct`` on both halves: the validating constructor re-runs
    # ``SealedBotProgram``'s nested-hash validator, which needs a whole
    # configured-signal seal this gate never reads.
    return BrokerBotBinding.model_construct(
        strategy_instance_id=sid,
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        use_rth=use_rth,
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        sealed_account_id=sealed,
        run_id="run-1",
        created_at_ms=0,
        sealed_program=_seal(sealed_account_id=sealed),
    )


def _clean_day(ledger: ShadowSessionLedger, *, day: date = DAY, opened_minute: int = 180) -> None:
    ledger.append(
        kind="day_opened",
        session_open_ms=session_open_ms_utc(day),
        observed_at_ms=et_minute_of_day_ms(day, opened_minute),
        verdict="clean",
    )
    ledger.append(
        kind="session_closed_clean",
        session_open_ms=session_open_ms_utc(day),
        observed_at_ms=et_minute_of_day_ms(day, 1200),
        verdict="clean",
    )


def _shadow_source(
    fills: Sequence[TwinFill] = (),
    runs: Sequence[RunResource] = (),
    *,
    unreadable: Sequence[date] = (),
) -> _Source:
    return _Source("shadow", SID, fills, runs, unreadable=unreadable)


def _twin_source(
    fills: Sequence[TwinFill] = (),
    runs: Sequence[RunResource] = (),
    *,
    unreadable: Sequence[date] = (),
) -> _Source:
    return _Source("paper twin", TWIN, fills, runs, unreadable=unreadable)


COVERING_TWIN_RUN = (_twin_run(OPEN - 1, None),)


def _evaluate(
    tmp_path: Path,
    *,
    ledger: ShadowSessionLedger,
    shadow: _Source,
    twin: _Source,
    required: int = 1,
    now_ms: int | None = None,
    use_rth: bool = True,
    window: ExtendedHoursWindow | None = ALPACA_EXTENDED_HOURS_WINDOW,
):
    return evaluate_shadow_gate(
        live_account_id="9LIVE0001",
        shadow_binding=_binding(SID, "shadow:9LIVE0001", use_rth=use_rth),
        twin_binding=_binding(TWIN, "PA-TEST", use_rth=use_rth),
        twin_account_id="PA-TEST",
        required_sessions=required,
        session_ledger=ledger,
        shadow_source=shadow,
        twin_source=twin,
        window=window,
        now_ms=et_minute_of_day_ms(date(2026, 9, 9), 60) if now_ms is None else now_ms,
    )


def test_a_clean_covered_reconciled_day_counts(tmp_path: Path) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger)
    shadow = _shadow_source([_fill(600)], [_run(OPEN - 1, None)])
    twin = _twin_source([_fill(600, ref="p")], COVERING_TWIN_RUN)

    evaluation = _evaluate(tmp_path, ledger=ledger, shadow=shadow, twin=twin)

    [verdict] = evaluation.sessions
    assert (verdict.state, verdict.shadow_run_id) == ("counted", "run-1")
    assert verdict.reconciliation is not None and verdict.reconciliation.passed
    assert evaluation.satisfied is True and evaluation.counted == (verdict,)


@pytest.mark.parametrize(
    "twin_runs",
    [
        pytest.param((), id="the-twin-never-ran"),
        pytest.param((_twin_run(OPEN + 1, None),), id="the-twin-started-after-the-open"),
        pytest.param((_twin_run(OPEN - 1, CLOSE - 1),), id="the-twin-stopped-before-the-close"),
    ],
)
def test_a_day_the_twin_was_not_running_does_not_count(
    tmp_path: Path, twin_runs: tuple[RunResource, ...]
) -> None:
    """A twin that did not run the day proves nothing, so the day cannot count.

    Both sides are silent, so ``reconcile_twin_day`` passes vacuously: two
    empty fill sets pair into zero divergences. Only the twin's own run
    coverage separates "the twins agreed to do nothing" from "the twin was
    switched off", and the receipt this day feeds later qualifies real-money
    arming (ADR 0059 D2/D4).
    """
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger)
    shadow = _shadow_source(runs=[_run(OPEN - 1, None)])
    twin = _twin_source(runs=twin_runs)

    evaluation = _evaluate(tmp_path, ledger=ledger, shadow=shadow, twin=twin)

    [verdict] = evaluation.sessions
    assert verdict.state == "run_not_covering"
    assert verdict.detail == "no run of the paper twin spanned the whole decision session"
    # The shadow side *did* cover the day, and the verdict still names its run.
    assert verdict.shadow_run_id == "run-1"
    assert evaluation.satisfied is False


@pytest.mark.parametrize(
    ("opened_minute", "run", "twin_fills", "state"),
    [
        (600, _run(OPEN - 1, None), [_fill(600, ref="p")], "sweep_opened_late"),
        (180, _run(OPEN + 1, None), [_fill(600, ref="p")], "run_not_covering"),
        (180, _run(OPEN - 1, CLOSE - 1), [_fill(600, ref="p")], "run_not_covering"),
        (180, _run(OPEN - 1, None), [], "twin_diverged"),
    ],
)
def test_days_that_do_not_count_say_why(
    tmp_path: Path,
    opened_minute: int,
    run: RunResource,
    twin_fills: list[TwinFill],
    state: str,
) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger, opened_minute=opened_minute)
    shadow = _shadow_source([_fill(600)], [run])
    # The twin covers every one of these days, so each row's named state is
    # the only reason the day did not count.
    twin = _twin_source(twin_fills, COVERING_TWIN_RUN)

    evaluation = _evaluate(tmp_path, ledger=ledger, shadow=shadow, twin=twin)

    assert [v.state for v in evaluation.sessions] == [state]
    assert evaluation.satisfied is False


def test_an_unreadable_twin_day_is_not_evaluable_and_leaves_the_other_days_judged(
    tmp_path: Path,
) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger, day=PRIOR_DAY)
    _clean_day(ledger)
    shadow = _shadow_source(
        [_fill(600, day=PRIOR_DAY), _fill(600)],
        [_run(session_open_ms_utc(PRIOR_DAY) - 1, None)],
    )
    twin = _twin_source(
        [_fill(600, day=PRIOR_DAY, ref="p"), _fill(600, ref="p")],
        [_twin_run(session_open_ms_utc(PRIOR_DAY) - 1, None)],
        unreadable=[PRIOR_DAY],
    )

    evaluation = _evaluate(tmp_path, ledger=ledger, shadow=shadow, twin=twin, now_ms=CLOSE + 1)

    unreadable, readable = evaluation.sessions
    assert (unreadable.state, unreadable.detail) == ("not_evaluable", UNREADABLE)
    assert unreadable.reconciliation is None and unreadable.shadow_run_id == "run-1"
    assert readable.state == "counted"
    assert evaluation.counted == (readable,)
    # Only the twin side is unreadable, and the shadow side was still asked:
    # the two worlds are separate readers, so one refusing cannot be mistaken
    # for both refusing.
    assert shadow.consulted == [SID, SID]
    assert twin.consulted == [TWIN, TWIN]


def test_an_unreadable_shadow_day_is_not_evaluable_before_the_twin_is_asked(
    tmp_path: Path,
) -> None:
    """The mirror of the case above, which one shared double could not express.

    The shadow authority refuses the window and the paper twin is perfectly
    readable; the day must still be ``not_evaluable``, and the twin must not
    be consulted for a day the shadow side already could not vouch for.
    """
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger)
    shadow = _shadow_source([_fill(600)], [_run(OPEN - 1, None)], unreadable=[DAY])
    twin = _twin_source([_fill(600, ref="p")], COVERING_TWIN_RUN)

    evaluation = _evaluate(tmp_path, ledger=ledger, shadow=shadow, twin=twin)

    [verdict] = evaluation.sessions
    assert (verdict.state, verdict.detail) == ("not_evaluable", UNREADABLE)
    assert evaluation.satisfied is False
    assert shadow.consulted == [SID]
    assert twin.consulted == []


DECLARED_OPEN_MINUTE = ALPACA_EXTENDED_HOURS_WINDOW.open_minute_et  # 04:00 ET


def test_an_extended_run_is_judged_against_its_declared_open_not_the_calendar_open(
    tmp_path: Path,
) -> None:
    # 05:00 ET is after the declared extended open and well before the calendar
    # open, so only the decision-span comparison can catch this sweep as late.
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger, opened_minute=300)
    shadow = _shadow_source(
        [_fill(600)], [_run(et_minute_of_day_ms(DAY, DECLARED_OPEN_MINUTE) - 1, None)]
    )
    twin = _twin_source(
        [_fill(600, ref="p")], [_twin_run(et_minute_of_day_ms(DAY, DECLARED_OPEN_MINUTE) - 1, None)]
    )

    under_rth = _evaluate(tmp_path, ledger=ledger, shadow=shadow, twin=twin)
    extended = _evaluate(tmp_path, ledger=ledger, shadow=shadow, twin=twin, use_rth=False)

    assert [v.state for v in under_rth.sessions] == ["counted"]
    [late] = extended.sessions
    assert late.state == "sweep_opened_late"
    assert late.session_open_ms == OPEN  # the journal key stays the calendar open


def test_an_extended_binding_with_no_declared_window_is_not_evaluable(tmp_path: Path) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger)
    shadow = _shadow_source([_fill(600)], [_run(OPEN - 1, None)])
    twin = _twin_source([_fill(600, ref="p")])

    evaluation = _evaluate(
        tmp_path, ledger=ledger, shadow=shadow, twin=twin, use_rth=False, window=None
    )

    [verdict] = evaluation.sessions
    assert verdict.state == "not_evaluable"
    assert verdict.detail == "an extended-session binding has no declared window to judge against"
    assert (verdict.session_open_ms, verdict.shadow_run_id) == (OPEN, None)
    assert evaluation.satisfied is False


@pytest.mark.parametrize(
    ("rows", "detail"),
    [
        ((), "the sweep never opened the day"),
        (("day_opened",), "the sweep did not close the day clean"),
    ],
)
def test_every_incomplete_sweep_says_which_half_is_missing(
    tmp_path: Path, rows: tuple[str, ...], detail: str
) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    for kind in rows:
        ledger.append(
            kind=kind,
            session_open_ms=OPEN,
            observed_at_ms=et_minute_of_day_ms(DAY, 180),
            verdict="clean",
        )
    shadow = _shadow_source(runs=[_run(OPEN - 1, None)])

    [verdict] = _evaluate(tmp_path, ledger=ledger, shadow=shadow, twin=_twin_source()).sessions

    assert (verdict.state, verdict.detail) == ("sweep_not_clean", detail)


@pytest.mark.parametrize(
    ("update", "match"),
    [
        ({"use_rth": False}, "session shape"),
        ({"sealed_program": _seal(sealed_account_id="PA-OTHER")}, "named twin account"),
    ],
)
def test_a_twin_that_is_not_this_instances_twin_is_refused(
    tmp_path: Path, update: dict[str, object], match: str
) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    shadow, twin = _shadow_source(), _twin_source()

    with pytest.raises(ShadowTwinMismatch, match=match):
        evaluate_shadow_gate(
            live_account_id="9LIVE0001",
            shadow_binding=_binding(SID, "shadow:9LIVE0001"),
            twin_binding=_binding(TWIN, "PA-TEST").model_copy(update=update),
            twin_account_id="PA-TEST",
            required_sessions=1,
            session_ledger=ledger,
            shadow_source=shadow,
            twin_source=twin,
            window=ALPACA_EXTENDED_HOURS_WINDOW,
            now_ms=CLOSE + 1,
        )


def test_a_binding_with_no_sealed_program_is_refused(tmp_path: Path) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    shadow, twin = _shadow_source(), _twin_source()

    with pytest.raises(ShadowTwinMismatch, match="sealed program"):
        evaluate_shadow_gate(
            live_account_id="9LIVE0001",
            shadow_binding=_binding(SID, "shadow:9LIVE0001").model_copy(
                update={"sealed_program": None}
            ),
            twin_binding=_binding(TWIN, "PA-TEST"),
            twin_account_id="PA-TEST",
            required_sessions=1,
            session_ledger=ledger,
            shadow_source=shadow,
            twin_source=twin,
            window=ALPACA_EXTENDED_HOURS_WINDOW,
            now_ms=CLOSE + 1,
        )


def test_a_non_clean_day_and_a_seal_mismatch_are_named(tmp_path: Path) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    ledger.append(
        kind="day_opened",
        session_open_ms=OPEN,
        observed_at_ms=et_minute_of_day_ms(DAY, 180),
        verdict="stale",
    )
    ledger.append(
        kind="non_clean",
        session_open_ms=OPEN,
        observed_at_ms=et_minute_of_day_ms(DAY, 180),
        verdict="stale",
    )
    shadow = _shadow_source(runs=[_run(OPEN - 1, None)])
    twin = _twin_source()
    assert [
        v.state
        for v in _evaluate(tmp_path, ledger=ledger, shadow=shadow, twin=twin).sessions
    ] == ["sweep_not_clean"]

    with pytest.raises(ShadowTwinMismatch, match="configured signal"):
        evaluate_shadow_gate(
            live_account_id="9LIVE0001",
            shadow_binding=_binding(SID, "shadow:9LIVE0001"),
            twin_binding=_binding(TWIN, "PA-TEST").model_copy(
                update={"sealed_program": _seal(configured_signal_hash="b" * 64)}
            ),
            twin_account_id="PA-TEST",
            required_sessions=1,
            session_ledger=ledger,
            shadow_source=shadow,
            twin_source=twin,
            window=ALPACA_EXTENDED_HOURS_WINDOW,
            now_ms=CLOSE + 1,
        )
