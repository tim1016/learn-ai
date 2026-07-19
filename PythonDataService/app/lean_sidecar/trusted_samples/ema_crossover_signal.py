"""LEAN validation twin for the canonical EMA Crossover Signal strategy.

Formula: Long-only EMA(5)/EMA(10) crossover on 15-minute signal bars, gated
by Wilders RSI(14), with a five-bar time exit.
Reference: ``Lean/Algorithm.CSharp/SpyEmaCrossoverAlgorithm.cs`` (Apr 2026
revision), preserved in this repository's validation provenance.
Canonical Python implementation:
``app.engine.strategy.algorithms.ema_crossover_signal.EmaCrossoverSignalAlgorithm``.
Validated against: the SPY/QQQ W3mo and W6mo cross-engine parity cells.

The LEAN runtime must execute a concrete equity order, whereas the canonical
Python strategy emits asset-agnostic ENTER/EXIT intents for the Action Plan
execution boundary. For parity, Engine Lab deliberately binds those intents
to the same signal symbol that this template subscribes to. Asset selection is
therefore outside this LEAN validation twin, rather than a second copy of the
strategy's signal logic.

The executable source is intentionally shared with ``ema_crossover``. The
rules are identical; this named export gives the migrated strategy an explicit
template identity without creating two independently drifting LEAN sources.
"""

from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE

EMA_CROSSOVER_SIGNAL_SOURCE = EMA_CROSSOVER_SOURCE
