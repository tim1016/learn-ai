# Patch — `bars.py` open-attribute fallback

## What

Fix `app/broker/ibkr/bars.py` so it reads OHLC fields from `ib_async.RealTimeBar` (which uses `open_` with a trailing underscore) as well as the test fakes (which use `open`).

## Why

Confirmed at `.venv/Lib/site-packages/ib_async/objects.py:113`:

```python
@dataclass
class RealTimeBar:
    time: datetime = EPOCH
    endTime: int = -1
    open_: float = 0.0   # ← trailing underscore
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
```

Codex's `bars.py:82-83`:

```python
def _decimal_attr(obj, name: str) -> Decimal:
    return Decimal(str(getattr(obj, name)))
```

Called as `_decimal_attr(bar, "open")` at `bars.py:115`. Production crashes on real ib_async bars because `getattr(real_bar, "open")` raises `AttributeError`. Tests pass because the test fake at `tests/broker/ibkr/test_bars.py:18-26` uses `SimpleNamespace(open=...)` (no underscore).

This blocks Phase 10 (paper week). It does NOT block the demo's replay parity test.

## Fix (drop-in replacement for `_decimal_attr`)

```python
def _decimal_attr(obj, *names: str) -> Decimal:
    """Read the first present attribute and coerce to ``Decimal``.

    The bar protocol differs slightly between sources:
    * ``ib_async.RealTimeBar`` exposes the open as ``open_`` (trailing
      underscore to avoid shadowing the ``open()`` builtin in dataclass
      code).
    * Test fakes use ``open`` directly because ``open`` is a built-in
      function, not a Python keyword, so naming an attribute that way
      is legal and reads more naturally.

    Try each candidate name in order; raise if none are present.
    """
    for name in names:
        if hasattr(obj, name):
            return Decimal(str(getattr(obj, name)))
    raise IBKRBarStreamError(f"5-second bar missing all of: {names!r}")
```

Then update the only field that needs the dual lookup (`open` only — `high`, `low`, `close` are spelled the same in both sources):

```python
# bars.py:115 — change from:
open_price = _decimal_attr(bar, "open")
# to:
open_price = _decimal_attr(bar, "open", "open_")
```

`high`, `low`, `close` calls at `bars.py:116-118` keep their single-name signature; they continue to work because the variadic accepts one name.

## Verification

After patch:

```bash
cd PythonDataService
source .venv/bin/activate
pytest tests/broker/ibkr/test_bars.py -v   # all 6 still pass
ruff check app/ tests/                       # clean
```

Then add a regression test in `tests/broker/ibkr/test_bars.py` that uses an `open_` attribute to lock the contract:

```python
def test_aggregate_handles_ib_async_open_underscore_attribute() -> None:
    """Regression: real ib_async.RealTimeBar uses 'open_' not 'open'."""
    raw = SimpleNamespace(
        time=datetime(2026, 5, 4, 14, 30, 0, tzinfo=UTC),
        open_=Decimal("100.00"),  # underscore, like ib_async
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
    )
    current, emitted, _ = aggregate_realtime_bar(
        None, raw, symbol="SPY", last_source_ms=None,
    )
    assert current is not None
    assert emitted is None
    minute = current.to_model()
    assert minute.open == Decimal("100.00")
```

## Time

Patch + regression test: ~5 minutes. Do this on the way to Phase 10, not before the demo (it has zero effect on the parity gate).
