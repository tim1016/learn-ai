"""LEAN validation twin for the configurable ``EMA Crossover 2 bps``.

The source is derived from the absolute-$0.20 oracle through audited,
fail-closed substitutions for fixture identity, runtime gate parameters, and
the relative-gap formula. Every unrelated LEAN behavior stays identical.
"""

from app.lean_sidecar.trusted_samples.ema_crossover import derive_two_bps_source

EMA_CROSSOVER_2_BPS_SOURCE = derive_two_bps_source()

__all__ = ["EMA_CROSSOVER_2_BPS_SOURCE"]
