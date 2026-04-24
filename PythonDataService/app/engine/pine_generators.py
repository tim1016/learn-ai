"""Pine v6 generators for engine strategies.

Each generator takes the strategy's validated Pydantic params and
returns a complete Pine v6 script, logically identical to the Python
port. Exposed via ``GET /api/engine/strategies/{name}/pine`` so the
frontend can download the script that matches the user's current
configuration.

Implementation philosophy:
  * One generator per strategy — they differ enough that a shared
    templating abstraction would obscure rather than help.
  * All generators use an ``f""`` block for the full script and
    substitute parameters directly. Pine v6 does not use ``{`` / ``}``
    outside of deprecated map literals we don't emit, so raw f-strings
    don't collide with Pine syntax.
  * The input defaults on the Pine ``input.*`` functions are set to
    the user's current parameter values, so the downloaded script
    runs as-is out of the box. Every Pine input is still editable in
    TradingView after download.
"""

from __future__ import annotations

from typing import Any


def _float(v: Any) -> str:
    """Format a number for Pine, always with a decimal point so it's
    treated as a float literal (Pine distinguishes ``1`` from ``1.0``)."""
    f = float(v)
    if f == int(f):
        return f"{int(f)}.0"
    return repr(f)


def generate_strategy_a_pine(p: Any) -> str:
    """Pine v6 script for ``rsi_range_a`` — EMA-gap + MACD + RSI-range."""
    return f"""//@version=6
// =====================================================================
// Strategy A — EMA-gap + MACD + RSI-range, ADX-exit
//
// Generated from PythonDataService/app/engine/strategy/algorithms/
// spy_strategy_a.py with the user's current parameter values as
// defaults. Every input remains editable in TradingView.
//
// Entry gates (all evaluated each bar while flat):
//   1. rsi_low_gate <= RSI <= rsi_high_gate
//   2. (EMA_fast - EMA_slow) > ema_gap_threshold
//   3. MACD line > 0
//
// Exit: ADX < adx_exit_threshold
//
// Long-only. 15-min bars. pyramiding=1. commission=0. slippage=0.
// No SL/TP.
// =====================================================================

strategy("Strategy A — EMA-gap + MACD + RSI-range",
     overlay         = true,
     pyramiding      = 1,
     default_qty_type = strategy.percent_of_equity,
     default_qty_value = 100,
     commission_type = strategy.commission.percent,
     commission_value = 0,
     slippage        = 0,
     process_orders_on_close = false)

// ---- Inputs -----------------------------------------------------------
emaFastLen      = input.int({p.ema_fast_period}, "EMA fast period", minval=2, group="EMAs")
emaSlowLen      = input.int({p.ema_slow_period}, "EMA slow period", minval=3, group="EMAs")
emaGapThreshold = input.float({_float(p.ema_gap_threshold)}, "EMA-gap threshold (fast − slow)", minval=0, group="EMAs",
     tooltip="Minimum absolute gap at the signal bar. Scales with ticker price.")

macdFast   = input.int({p.macd_fast},   "MACD fast",   minval=2, group="MACD")
macdSlow   = input.int({p.macd_slow},   "MACD slow",   minval=3, group="MACD")
macdSignal = input.int({p.macd_signal}, "MACD signal", minval=2, group="MACD")

rsiLen      = input.int({p.rsi_period}, "RSI period",  minval=2, group="RSI range")
rsiLowGate  = input.float({_float(p.rsi_low_gate)},  "RSI low gate",  minval=0, maxval=100, group="RSI range")
rsiHighGate = input.float({_float(p.rsi_high_gate)}, "RSI high gate", minval=0, maxval=100, group="RSI range")

adxLen       = input.int({p.adx_period}, "ADX period", minval=2, group="ADX")
adxExitThres = input.float({_float(p.adx_exit_threshold)}, "ADX exit threshold", minval=0, maxval=100, group="ADX")

// ---- Indicators -------------------------------------------------------
emaFast = ta.ema(close, emaFastLen)
emaSlow = ta.ema(close, emaSlowLen)

[macdLine, _signalLine, _histLine] = ta.macd(close, macdFast, macdSlow, macdSignal)
rsi = ta.rsi(close, rsiLen)
[_diPlus, _diMinus, adx] = ta.dmi(adxLen, adxLen)

// ---- Entry gate -------------------------------------------------------
rsiInRange = rsi >= rsiLowGate and rsi <= rsiHighGate
emaGap     = emaFast - emaSlow
entryOK    = rsiInRange and emaGap > emaGapThreshold and macdLine > 0

// ---- Order management -------------------------------------------------
inPos = strategy.position_size > 0

if entryOK and not inPos
    strategy.entry("Long", strategy.long)

if inPos and adx < adxExitThres
    strategy.close("Long", comment="ADX exit")

// ---- Plots ------------------------------------------------------------
plot(emaFast, "EMA fast", color=color.new(color.blue, 0))
plot(emaSlow, "EMA slow", color=color.new(color.orange, 0))
"""


def generate_strategy_b_pine(p: Any) -> str:
    """Pine v6 script for ``rsi_range_b`` — Supertrend + ADX + MACD + RSI-range."""
    return f"""//@version=6
// =====================================================================
// Strategy B — Supertrend + ADX + MACD + RSI-range
//
// Entry gates (all each bar while flat):
//   1. rsi_low_gate <= RSI <= rsi_high_gate
//   2. Supertrend is long (price above the line)
//   3. ADX > adx_entry_threshold
//   4. MACD line > 0
//
// Exit: ADX < adx_exit_threshold
//
// NOTE: Pine's ta.supertrend direction sign is -1 = uptrend / +1 =
// downtrend. The Python port uses the opposite (pandas-ta) convention.
// This script maps via ``stIsLong = stDir == -1`` to express "is long"
// identically.
// =====================================================================

strategy("Strategy B — Supertrend + ADX + MACD + RSI-range",
     overlay         = true,
     pyramiding      = 1,
     default_qty_type = strategy.percent_of_equity,
     default_qty_value = 100,
     commission_type = strategy.commission.percent,
     commission_value = 0,
     slippage        = 0)

// ---- Inputs -----------------------------------------------------------
stAtrLen = input.int({p.supertrend_atr_period}, "Supertrend ATR length", minval=2, group="Supertrend")
stMult   = input.float({_float(p.supertrend_multiplier)}, "Supertrend multiplier", minval=0.1, group="Supertrend")

adxLen         = input.int({p.adx_period}, "ADX period", minval=2, group="ADX")
adxEntryThres  = input.float({_float(p.adx_entry_threshold)}, "ADX entry threshold", minval=0, maxval=100, group="ADX")
adxExitThres   = input.float({_float(p.adx_exit_threshold)}, "ADX exit threshold",  minval=0, maxval=100, group="ADX")

macdFast   = input.int({p.macd_fast}, "MACD fast",   minval=2, group="MACD")
macdSlow   = input.int({p.macd_slow}, "MACD slow",   minval=3, group="MACD")
macdSignal = input.int({p.macd_signal}, "MACD signal", minval=2, group="MACD")

rsiLen      = input.int({p.rsi_period}, "RSI period",  minval=2, group="RSI range")
rsiLowGate  = input.float({_float(p.rsi_low_gate)},  "RSI low gate",  minval=0, maxval=100, group="RSI range")
rsiHighGate = input.float({_float(p.rsi_high_gate)}, "RSI high gate", minval=0, maxval=100, group="RSI range")

// ---- Indicators -------------------------------------------------------
[stLine, stDir] = ta.supertrend(stMult, stAtrLen)
stIsLong = stDir == -1

[_diPlus, _diMinus, adx] = ta.dmi(adxLen, adxLen)
[macdLine, _signalLine, _hist] = ta.macd(close, macdFast, macdSlow, macdSignal)
rsi = ta.rsi(close, rsiLen)

// ---- Entry gate -------------------------------------------------------
rsiInRange = rsi >= rsiLowGate and rsi <= rsiHighGate
entryOK = rsiInRange and stIsLong and adx > adxEntryThres and macdLine > 0

// ---- Order management -------------------------------------------------
inPos = strategy.position_size > 0

if entryOK and not inPos
    strategy.entry("Long", strategy.long)

if inPos and adx < adxExitThres
    strategy.close("Long", comment="ADX exit")

// ---- Plots ------------------------------------------------------------
plot(stLine, "Supertrend", color = stIsLong ? color.new(color.green, 0) : color.new(color.red, 0))
"""


def generate_strategy_c_pine(p: Any) -> str:
    """Pine v6 script for ``rsi_range_c`` — ADX-rising + RSI-range."""
    return f"""//@version=6
// =====================================================================
// Strategy C — ADX-rising + RSI-range
//
// Entry gates (all each bar while flat):
//   1. rsi_low_gate <= RSI <= rsi_high_gate
//   2. ADX > adx_entry_threshold
//   3. ADX > ADX[1] (bar-over-bar rising)
//
// Exit: ADX < adx_exit_threshold
// =====================================================================

strategy("Strategy C — ADX-rising + RSI-range",
     overlay         = true,
     pyramiding      = 1,
     default_qty_type = strategy.percent_of_equity,
     default_qty_value = 100,
     commission_type = strategy.commission.percent,
     commission_value = 0,
     slippage        = 0)

// ---- Inputs -----------------------------------------------------------
adxLen         = input.int({p.adx_period}, "ADX period", minval=2, group="ADX")
adxEntryThres  = input.float({_float(p.adx_entry_threshold)}, "ADX entry threshold", minval=0, maxval=100, group="ADX")
adxExitThres   = input.float({_float(p.adx_exit_threshold)}, "ADX exit threshold",  minval=0, maxval=100, group="ADX")

rsiLen      = input.int({p.rsi_period}, "RSI period",  minval=2, group="RSI range")
rsiLowGate  = input.float({_float(p.rsi_low_gate)},  "RSI low gate",  minval=0, maxval=100, group="RSI range")
rsiHighGate = input.float({_float(p.rsi_high_gate)}, "RSI high gate", minval=0, maxval=100, group="RSI range")

// ---- Indicators -------------------------------------------------------
[_diPlus, _diMinus, adx] = ta.dmi(adxLen, adxLen)
rsi = ta.rsi(close, rsiLen)

// ---- Entry gate -------------------------------------------------------
prevAdx = adx[1]
adxRising  = not na(prevAdx) and adx > prevAdx
rsiInRange = rsi >= rsiLowGate and rsi <= rsiHighGate
entryOK    = rsiInRange and adx > adxEntryThres and adxRising

// ---- Order management -------------------------------------------------
inPos = strategy.position_size > 0

if entryOK and not inPos
    strategy.entry("Long", strategy.long)

if inPos and adx < adxExitThres
    strategy.close("Long", comment="ADX exit")
"""
