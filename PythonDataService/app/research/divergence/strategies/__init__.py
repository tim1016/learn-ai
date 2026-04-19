"""Strategy implementations for the divergence study.

Three rule-sets, each implemented as a pure pandas function that consumes a
DataFrame with indicator columns and returns a list of :class:`Trade`
records. Strategy logic is long-only, bar-close fills, no commissions.

  * S1 — EMA(5/10) crossover + RSI(14) filter. 5-bar hold.
  * S2 — RSI(14) mean-reversion. Enter <30, exit >50 or after 20 bars.
  * S3 — SMA(50/200) golden / death cross.
"""

from app.research.divergence.strategies.common import Trade, TradeList
from app.research.divergence.strategies.s1_ema_crossover import run_s1_ema_crossover
from app.research.divergence.strategies.s2_rsi_mean_reversion import run_s2_rsi_mean_reversion
from app.research.divergence.strategies.s3_sma_crossover import run_s3_sma_crossover

__all__ = [
    "Trade",
    "TradeList",
    "run_s1_ema_crossover",
    "run_s2_rsi_mean_reversion",
    "run_s3_sma_crossover",
]
