"""LEAN deci-cent CSV-in-zip writer.

Spec: docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md § 5.1
Reference for the on-disk format: PythonDataService/app/engine/data/lean_format.py
(existing writer; this module supersedes it inside the data lake but does not
remove the existing one until Slice 1d).

LEAN minute-trade zip layout (path constructed by app.data_lake.path_policy):
    <yyyymmdd>_trade.zip
      └── <yyyymmdd>_<sym_lower>_minute_trade.csv
           no header; columns:
             ms_since_midnight_et, open*10000, high*10000, low*10000,
             close*10000, volume (shares, exact — see format_volume)
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

# LEAN's price scale factor: prices on disk are multiplied by 10_000.
_PRICE_SCALE = Decimal(10_000)
_QUANT = Decimal(1)  # round to integer after scaling

# What format_volume writes: whole shares, or plain notation with no trailing
# zero. parse_volume reads nothing else, so an exponent, sign or padding
# (which LEAN's GetDecimal misreads) is refused, as int() refused it before.
_VOLUME_COLUMN = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]*[1-9])?")

# ZIP archive epoch — pinned so two runs with identical inputs produce
# byte-identical zips. ZipFile default is "now", which would break the
# data_availability_hash determinism gate.
_DETERMINISTIC_ZIP_DATE_TIME: tuple[int, int, int, int, int, int] = (
    1980,
    1,
    1,
    0,
    0,
    0,
)


@dataclass(frozen=True)
class MinuteTradeBar:
    """One minute trade bar in exchange-local (ET) wall clock.

    bar_start_et is the inclusive start of the minute (e.g. 09:30:00 ET
    represents the [09:30:00, 09:31:00) bar). LEAN's CSV column 0 is
    ms_since_midnight_et computed from this value.
    """

    bar_start_et: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def to_deci_cent(price: Decimal) -> int:
    """Encode a price as our on-disk integer, on LEAN's 1/10,000 grid.
    **Canonical for this repo.**

    Formula: ``round_half_up(price * 10_000)``.
    Reference: LEAN's scale factor (``TradeBar._scaleFactor = 1/10000m`` in
    ``Common/Data/Market/TradeBar.cs``) fixes the grid; LEAN's writer
    (``LeanData.Scale`` in ``Common/Util/LeanData.cs``) does ``value *
    10_000m`` and formats it with trailing zeros stripped — **it does not
    round or truncate to an integer**. So a price already on the grid (true
    for the overwhelming majority of Polygon-sourced data) round-trips
    byte-identical either way; a price finer than the grid is where our port
    diverges from a literal replication, because our on-disk format requires
    an integer field and LEAN's does not. The half-up rule below is
    therefore our own quantization decision, not a proven LEAN behavior —
    see ADR 0069 §8 (accepted departures) for the decision
    and its receipt.
    Canonical implementation: this function. Every writer in the tree encodes
    through it — the lake's own zip builders (``build_minute_trade_zip_bytes``,
    ``app.data_lake.derived_daily``, ``app.data_lake.derived_quote``) and the
    pre-lake policy-store writers in ``app.engine.data.lean_format``.
    Validated against: ``tests/unit/data_lake/test_deci_cent_canonical.py``,
    which pins the rule and asserts both writers agree bar-for-bar on prices
    finer than the deci-cent grid — an internal cross-writer-consistency
    proof, not a LEAN-equivalence proof (there is no LEAN rounding rule to
    be equivalent to).

    **Why half-up rather than truncation.** Encoding is a quantization onto
    the deci-cent grid, so the only defensible target is the nearest
    representable value. ``int(price * 10_000)`` truncates toward zero, which
    is not a rounding rule but a systematic downward bias: its error is
    uniform on ``[0, 1)`` deci-cents with expected value ``-0.5`` on every
    OHLC field of every bar, where half-up's error is symmetric on
    ``[-0.5, 0.5]`` with expected value ``0``. A bias that never changes sign
    accumulates through the strategy; symmetric error does not. The magnitude
    is one deci-cent ($0.0001) and it can only appear on a price finer than
    the grid, which for US equities means a sub-$1.00 name (Reg NMS permits a
    $0.0001 tick below $1.00) or a provider revision carrying more precision
    than the tape — narrow, but real, and it is the difference between two
    writers that agree by construction and two that agree by luck.

    Rejects negative prices (LEAN never serializes them; a negative would
    indicate upstream data corruption). ``app.data_lake.cache_import`` mirrors
    the refusal at its own decode boundary.
    """
    if price < 0:
        raise ValueError(f"deci-cent encoding refuses negative price: {price}")
    return int((price * _PRICE_SCALE).quantize(_QUANT, rounding=ROUND_HALF_UP))


def format_volume(volume: Decimal | int) -> str:
    """Encode a volume as the on-disk volume column: shares, exact.

    Since 2026-02-23 the SIPs report fractional shares, so a volume may carry
    up to six decimals. LEAN parses the column as a decimal
    (``TradeBar.ParseEquity`` in ``Common/Data/Market/TradeBar.cs``), so a
    fractional value stays LEAN-readable. A whole volume writes with no
    decimal point — byte-identical to every file written before fractional
    volume existed — and a fractional one in plain notation with trailing
    zeros stripped, so equal volumes always encode to equal bytes
    (``Decimal("1.0")`` and ``1`` both write ``1``).

    Canonical for this repo: every lake and policy-store writer encodes
    through it, and :func:`parse_volume` is its inverse. A float is refused —
    its binary expansion is not the vendor's number.
    """
    if isinstance(volume, float) or not isinstance(volume, Decimal | int):
        raise TypeError(f"volume encoding needs a Decimal or int, got {type(volume).__name__}")
    exact = Decimal(volume)
    if not exact.is_finite() or exact < 0:
        raise ValueError(f"volume encoding refuses {volume}: not finite and non-negative")
    if exact == exact.to_integral_value():
        return str(int(exact))
    return format(exact.normalize(), "f")


def parse_volume(text: str) -> Decimal:
    """Decode an on-disk volume column — the inverse of :func:`format_volume`.

    Accepts exactly what :func:`format_volume` writes.
    """
    if _VOLUME_COLUMN.fullmatch(text) is None:
        raise ValueError(f"volume {text!r} is not a whole or plain decimal share count")
    return Decimal(text)


def _ms_since_midnight_et(bar_start_et: datetime) -> int:
    """ms from midnight in the bar's tz (the bar_start_et is expected ET-aware)."""
    midnight = bar_start_et.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = bar_start_et - midnight
    return int(delta.total_seconds() * 1000)


def build_minute_trade_zip_bytes(
    symbol: str,
    trading_date_yyyymmdd: str,
    bars: list[MinuteTradeBar],
) -> bytes:
    """Build the deci-cent zip payload for a single (symbol, trading_date).

    Deterministic: same inputs produce byte-identical output. Caller writes
    the result via app.data_lake.atomic.atomic_write_and_promote.
    """
    sym_lower = symbol.lower()
    csv_name = f"{trading_date_yyyymmdd}_{sym_lower}_minute_trade.csv"
    lines = [
        ",".join(
            (
                str(_ms_since_midnight_et(bar.bar_start_et)),
                str(to_deci_cent(bar.open)),
                str(to_deci_cent(bar.high)),
                str(to_deci_cent(bar.low)),
                str(to_deci_cent(bar.close)),
                format_volume(bar.volume),
            )
        )
        for bar in bars
    ]
    csv_body = "\n".join(lines) + ("\n" if lines else "")

    buf = io.BytesIO()
    info = zipfile.ZipInfo(filename=csv_name, date_time=_DETERMINISTIC_ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, csv_body)
    return buf.getvalue()
