"""Declared source paths of every registered Signal Program — pure data.

The path tuples below used to live inline in ``registry.py``. #2450 moved
them here because the build-proof source anchor
(``app.services.program_source_anchor``) must enumerate the declared sources
and hash them BEFORE any of those sources is imported, and importing the
registry executes its module-scope program imports (the factories, the
programs, ``signal_program`` itself). This module therefore deliberately
has no imports of its own — keep it that way: any import here is an import
the anchor silently executes before it has hashed the declared bytes, which
reopens the exact window (#2450 review) the anchor exists to close.

``registry.py`` constructs its ``SignalProgramContract`` values from these
constants — the same objects, not a re-derived copy — so this stays the one
authority for what a proof may hash. The triage comments moved with the
tuples they explain.
"""

from __future__ import annotations

EMA_CROSSOVER_SIGNAL_ARTIFACT_PATHS: tuple[str, ...] = (
    # Issue #1728 defect 2: this is not "every file the module graph
    # reaches" — it is that transitive first-party import closure of the
    # two roots below, MINUS the files proven (in
    # scripts/run_signal_program_build_qualification.py's exclusion list)
    # to be unreachable from evaluate_signal_bar()'s decision math:
    # SignalSession.advance() (signal_program.py) builds and stores the
    # EvaluationTrace *before* settle() ever calls
    # commit_signal_decision(), so execution/fill/sizing/commission/
    # Insight-publication bytes reached only through the commit path
    # cannot retroactively change a trace that already exists.
    # test_signal_decision_digest_closure.py recomputes the closure from
    # these paths and fails the build if a newly introduced import isn't
    # triaged into this list or that one.
    "app/engine/strategy/algorithms/ema_crossover_signal.py",
    "app/engine/strategy/base.py",
    "app/engine/strategy/normalized_gap.py",
    "app/engine/strategy/signal_intent.py",
    "app/engine/strategy/signal_program.py",
    "app/engine/indicators/base.py",
    "app/engine/indicators/ema.py",
    "app/engine/indicators/rsi.py",
    "app/engine/indicators/sma.py",
    "app/engine/consolidators/trade_bar_consolidator.py",
    "app/engine/data/trade_bar.py",
    "app/engine/live/indicator_state.py",
    "app/lean_sidecar/trading_calendar.py",
    "app/utils/timestamps.py",
)
EMA_CROSSOVER_SIGNAL_WIRING_PATHS: tuple[str, ...] = (
    "app/engine/strategy/programs/ema_crossover_signal.py",
    "app/engine/strategy/params.py",
)

SMA_CROSSOVER_ARTIFACT_PATHS: tuple[str, ...] = (
    # Same triage rule as ema_crossover_signal's artifact_paths (issue
    # #1728 defect 2): the transitive first-party import closure of the
    # root below, MINUS the files in _SMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS
    # (scripts/run_signal_program_build_qualification.py) that are provably
    # unreachable from evaluate_signal_bar()'s decision math.
    # test_sma_signal_decision_digest_closure.py recomputes the closure from
    # these paths and fails the build if a newly introduced import isn't
    # triaged into one bucket or the other.
    "app/engine/strategy/algorithms/sma_crossover.py",
    "app/engine/strategy/base.py",
    "app/engine/strategy/signal_intent.py",
    "app/engine/strategy/signal_program.py",
    "app/engine/indicators/base.py",
    "app/engine/indicators/sma.py",
    "app/engine/consolidators/trade_bar_consolidator.py",
    "app/engine/data/trade_bar.py",
    "app/engine/live/indicator_state.py",
    "app/lean_sidecar/trading_calendar.py",
    "app/utils/timestamps.py",
)
SMA_CROSSOVER_WIRING_PATHS: tuple[str, ...] = (
    "app/engine/strategy/programs/sma_crossover.py",
    "app/engine/strategy/params.py",
)

RSI_MEAN_REVERSION_ARTIFACT_PATHS: tuple[str, ...] = (
    # Same triage rule as ema_crossover_signal's and sma_crossover's
    # artifact_paths (issue #1728 defect 2): the transitive first-party
    # import closure of the root below, MINUS the files in
    # _RSI_SIGNAL_DECISION_CLOSURE_EXCLUSIONS
    # (scripts/run_signal_program_build_qualification.py) that are provably
    # unreachable from evaluate_signal_bar()'s decision math.
    # test_rsi_signal_decision_digest_closure.py recomputes the closure and
    # fails the build if a newly introduced import isn't triaged into one
    # bucket or the other.
    "app/engine/strategy/algorithms/rsi_mean_reversion.py",
    "app/engine/strategy/base.py",
    "app/engine/strategy/signal_intent.py",
    "app/engine/strategy/signal_program.py",
    "app/engine/indicators/base.py",
    "app/engine/indicators/rsi.py",
    "app/engine/consolidators/trade_bar_consolidator.py",
    "app/engine/data/trade_bar.py",
    "app/engine/live/indicator_state.py",
    "app/lean_sidecar/trading_calendar.py",
    "app/utils/timestamps.py",
)
RSI_MEAN_REVERSION_WIRING_PATHS: tuple[str, ...] = (
    "app/engine/strategy/programs/rsi_mean_reversion.py",
    "app/engine/strategy/params.py",
)

DEPLOYMENT_VALIDATION_ARTIFACT_PATHS: tuple[str, ...] = (
    "app/engine/strategy/algorithms/deployment_validation.py",
    "app/engine/strategy/base.py",
    "app/engine/strategy/signal_intent.py",
    "app/engine/strategy/signal_program.py",
    "app/engine/consolidators/trade_bar_consolidator.py",
    "app/engine/data/trade_bar.py",
    "app/engine/live/indicator_state.py",
    "app/lean_sidecar/trading_calendar.py",
    "app/utils/timestamps.py",
)
DEPLOYMENT_VALIDATION_WIRING_PATHS: tuple[str, ...] = (
    "app/engine/strategy/programs/deployment_validation.py",
    "app/engine/strategy/params.py",
)

SPY_STRATEGY_A_ARTIFACT_PATHS: tuple[str, ...] = (
    # Same triage rule as sma_crossover's artifact_paths (issue #1728 defect
    # 2): the transitive first-party import closure of the two roots below,
    # MINUS the files in _SPY_STRATEGY_A_SIGNAL_DECISION_CLOSURE_EXCLUSIONS
    # (scripts/run_signal_program_build_qualification.py) that are provably
    # unreachable from evaluate_signal_bar()'s decision math.
    # test_spy_strategy_a_signal_decision_digest_closure.py recomputes the
    # closure from these paths and fails the build if a newly introduced
    # import isn't triaged into one bucket or the other.
    "app/engine/strategy/algorithms/spy_strategy_a.py",
    "app/engine/strategy/algorithms/_rsi_range_base.py",
    "app/engine/strategy/base.py",
    "app/engine/strategy/signal_intent.py",
    "app/engine/strategy/signal_program.py",
    "app/engine/indicators/base.py",
    "app/engine/indicators/ema.py",
    "app/engine/indicators/sma.py",
    "app/engine/indicators/macd.py",
    "app/engine/indicators/rsi.py",
    "app/engine/indicators/adx.py",
    "app/engine/consolidators/trade_bar_consolidator.py",
    "app/engine/data/trade_bar.py",
    "app/engine/live/indicator_state.py",
    "app/lean_sidecar/trading_calendar.py",
    "app/utils/timestamps.py",
)
SPY_STRATEGY_A_WIRING_PATHS: tuple[str, ...] = (
    "app/engine/strategy/programs/spy_strategy_a.py",
    "app/engine/strategy/params.py",
)

SPY_STRATEGY_B_ARTIFACT_PATHS: tuple[str, ...] = (
    # Same triage rule as ema_crossover_signal's / sma_crossover's own
    # artifact_paths (issue #1728 defect 2): the transitive first-party
    # import closure of the two roots below, MINUS the files in
    # _SPY_STRATEGY_B_SIGNAL_DECISION_CLOSURE_EXCLUSIONS
    # (scripts/run_signal_program_build_qualification.py) that are provably
    # unreachable from evaluate_signal_bar()'s decision math.
    # test_spy_strategy_b_signal_decision_digest_closure.py recomputes the
    # closure from these paths and fails the build if a newly introduced
    # import isn't triaged into one bucket or the other. indicators/ema.py
    # and indicators/sma.py are both real, non-obvious members of this
    # closure: macd.py's fast/slow lines are ExponentialMovingAverage
    # instances, and ema.py itself seeds its warmup from
    # SimpleMovingAverage -- both are load-bearing for the MACD entry gate,
    # not padding.
    "app/engine/strategy/algorithms/spy_strategy_b.py",
    "app/engine/strategy/algorithms/_rsi_range_base.py",
    "app/engine/strategy/base.py",
    "app/engine/strategy/signal_intent.py",
    "app/engine/strategy/signal_program.py",
    "app/engine/indicators/base.py",
    "app/engine/indicators/rsi.py",
    "app/engine/indicators/adx.py",
    "app/engine/indicators/supertrend.py",
    "app/engine/indicators/macd.py",
    "app/engine/indicators/ema.py",
    "app/engine/indicators/sma.py",
    "app/engine/consolidators/trade_bar_consolidator.py",
    "app/engine/data/trade_bar.py",
    "app/engine/live/indicator_state.py",
    "app/lean_sidecar/trading_calendar.py",
    "app/utils/timestamps.py",
)
SPY_STRATEGY_B_WIRING_PATHS: tuple[str, ...] = (
    "app/engine/strategy/programs/spy_strategy_b.py",
    "app/engine/strategy/params.py",
)

SPY_STRATEGY_C_ARTIFACT_PATHS: tuple[str, ...] = (
    # Same triage rule as ema_crossover_signal's/sma_crossover's
    # artifact_paths (issue #1728 defect 2): the transitive first-party
    # import closure of the root below, MINUS the files in
    # _SPY_C_SIGNAL_DECISION_CLOSURE_EXCLUSIONS
    # (scripts/run_signal_program_build_qualification.py) that are provably
    # unreachable from evaluate_signal_bar()'s decision math.
    # test_spy_strategy_c_signal_decision_digest_closure.py recomputes the
    # closure from these paths and fails the build if a newly introduced
    # import isn't triaged into one bucket or the other. RsiRangeStrategy
    # (the shared base) is a root alongside the leaf module because
    # evaluate_signal_bar/commit_signal_decision/discard_signal_decision all
    # live on the base, not on SpyStrategyCAlgorithm itself.
    "app/engine/strategy/algorithms/spy_strategy_c.py",
    "app/engine/strategy/algorithms/_rsi_range_base.py",
    "app/engine/strategy/base.py",
    "app/engine/strategy/signal_intent.py",
    "app/engine/strategy/signal_program.py",
    "app/engine/indicators/base.py",
    "app/engine/indicators/adx.py",
    "app/engine/indicators/rsi.py",
    "app/engine/consolidators/trade_bar_consolidator.py",
    "app/engine/data/trade_bar.py",
    "app/engine/live/indicator_state.py",
    "app/lean_sidecar/trading_calendar.py",
    "app/utils/timestamps.py",
)
SPY_STRATEGY_C_WIRING_PATHS: tuple[str, ...] = (
    "app/engine/strategy/programs/spy_strategy_c.py",
    "app/engine/strategy/params.py",
)

# Every service-relative source file a build proof may hash: the anchor
# (#2450) hashes exactly this union before any of it is imported, so a new
# program registered without a declaration here leaves its files unanchored
# and every proof for it refuses (unrecorded paths count as drift) rather
# than proving an unverified build.
DECLARED_PROGRAM_SOURCE_PATHS: frozenset[str] = frozenset(
    path
    for paths in (
        EMA_CROSSOVER_SIGNAL_ARTIFACT_PATHS,
        EMA_CROSSOVER_SIGNAL_WIRING_PATHS,
        SMA_CROSSOVER_ARTIFACT_PATHS,
        SMA_CROSSOVER_WIRING_PATHS,
        RSI_MEAN_REVERSION_ARTIFACT_PATHS,
        RSI_MEAN_REVERSION_WIRING_PATHS,
        DEPLOYMENT_VALIDATION_ARTIFACT_PATHS,
        DEPLOYMENT_VALIDATION_WIRING_PATHS,
        SPY_STRATEGY_A_ARTIFACT_PATHS,
        SPY_STRATEGY_A_WIRING_PATHS,
        SPY_STRATEGY_B_ARTIFACT_PATHS,
        SPY_STRATEGY_B_WIRING_PATHS,
        SPY_STRATEGY_C_ARTIFACT_PATHS,
        SPY_STRATEGY_C_WIRING_PATHS,
    )
    for path in paths
)
