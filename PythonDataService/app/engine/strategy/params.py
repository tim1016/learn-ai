"""The parameter base every strategy's model derives from, and the decision clock.

A leaf: the per-program modules under ``programs/`` and the registry both
import it, and it imports no strategy of its own. That is what lets a
program's executable closure name its own wiring without dragging the whole
registry -- and its eleven unrelated algorithm imports -- in behind it
(issue #1735).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StrategyParamsBase(BaseModel):
    """Base for every strategy's parameter model.

    Subclasses declare the strategy's own fields on top of ``symbol``.

    ``symbol`` is declared here rather than restated by each subclass
    (issue #1736) because every strategy names a signal stream and every
    caller reads it -- ``bot_trade_strategy`` to resolve a binding's default
    symbol, ``paper_deploy_service`` to exclude it from the deploy form's
    defaults. Declared only on the subclasses, those reads were
    ``# type: ignore[attr-defined]`` on an attribute that always existed,
    applied inconsistently across otherwise identical accesses.

    Deliberately required and un-defaulted: subclasses supply the default
    that makes sense for their own qualified corpus, and a new parameter
    model that forgets ``symbol`` should fail loudly at construction rather
    than silently inherit some other strategy's ticker.
    """

    model_config = {"extra": "forbid"}

    symbol: str


_DECISION_CLOCK_FIELD = "resolution_minutes"


def decision_timeframe_ms_for(params: StrategyParamsBase, *, qualified_ms: int) -> int:
    """The decision clock one resolved parameter set implies.

    A program is deploy-configurable exactly when its parameter model
    declares ``resolution_minutes``; one that does not runs the cadence its
    contract was qualified at. The parameter model is the declaration, so
    this reads the declaration rather than keeping a second list of which
    programs are tunable.

    Both the registry factories and
    ``app.services.signal_program_admission.build_start_program_seal`` call
    this. That matters: the seal hashes the cadence it claims the bot will
    run, so a second copy of this arithmetic anywhere is precisely the drift
    the seal exists to detect. Five factories previously each computed
    ``resolution_minutes * 60_000`` inline, and admission built an entire
    strategy object graph purely to read the result back off the session.
    """
    if _DECISION_CLOCK_FIELD not in type(params).model_fields:
        return qualified_ms
    return int(getattr(params, _DECISION_CLOCK_FIELD)) * 60_000


class EmaCrossoverParams(StrategyParamsBase):
    """EMA crossover signal parameters.

    Shares the exact indicator / gap / RSI logic as the LEAN-parity SPY
    reference run, but lets the user pick the *signal stream* at request time.
    Defaults to SPY so the out-of-the-box run matches the bit-exact
    reference fixture; other symbols (QQQ, IWM, etc.) can be substituted
    without touching the strategy. A live Action Plan independently selects
    the stock to trade; Engine Lab backtests bind the one loaded price stream
    to both roles.
    """

    symbol: str = Field(
        "SPY",
        min_length=1,
        max_length=20,
        description="Signal-stream ticker. The live Action Plan selects the traded stock separately.",
    )
