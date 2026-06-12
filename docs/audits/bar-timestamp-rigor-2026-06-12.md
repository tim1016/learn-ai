# Bar-timestamp rigor — Slice 4/5/6 audit (2026-06-12)

This note records the timestamp invariants the Slice 4 ``BarPersistence``
+ Slice 5 ``/chart-snapshot`` work depends on, and the regression test
that pins them.

## Scope

* Persistence layer: ``PythonDataService/app/services/bar_persistence.py``
* Live aggregator: ``PythonDataService/app/services/live_bar_aggregator.py``
* IBKR stream consolidator: ``PythonDataService/app/broker/ibkr/bars.py``
* Chart endpoint: ``PythonDataService/app/routers/live_instances.py::get_chart_snapshot``

## Invariants (the contract)

Every ``IbkrMinuteBar`` that survives the consolidator boundary and
lands in persistence carries:

1. **``start_ms`` is int64 ms UTC, inclusive.** No tz strings, no naive
   ``datetime`` — see ``.claude/rules/numerical-rigor.md`` →
   "Timestamp rigor".
2. **``end_ms`` is int64 ms UTC, exclusive.**
3. **``end_ms - start_ms == window_ms``** for the bar's resolution:
   * 1-min bars: ``60_000``.
   * 5-second bars: ``5_000``.
4. **``start_ms`` is aligned to the resolution's window.** A 1-min bar
   has ``start_ms % 60_000 == 0``; a 5-sec bar has
   ``start_ms % 5_000 == 0``.
5. **Monotonic, no silent gaps.** The persistence layer raises
   ``BarPersistenceRegressionError`` on a regression and quarantines
   the day's JSONL — duplicates and out-of-order bars surface, they are
   not papered over.

Invariant (3) is what the **partial-bar guard** in
``LiveBarAggregator._pump`` (Slice 4) enforces at the
stream/consolidator boundary: a restart mid-minute lets the
consolidator close an under-filled first minute with
``end_ms - start_ms < 60_000`` — that first bar is dropped (and not
persisted) so the chart never shows a ragged short candle.

Invariant (5) is why ``BarPersistence`` keeps an in-memory cursor + a
JSONL-resumption path: a fresh process pointed at an existing
directory rebuilds the monotonicity guard from the last persisted bar
so a restart can't sneak in an earlier ``start_ms``.

## How the two fields stay aligned

* **Producer side**: ``stream_minute_bars`` builds bars from a
  ``_MinuteAccumulator`` whose ``start_ms`` is the aligned minute and
  whose ``to_model()`` sets ``end_ms = start_ms + 60_000`` exactly.
  ``stream_raw_5s_bars`` reads raw 5-second IBKR bars and writes
  ``end_ms = start_ms + 5_000`` at the source-bar boundary.
* **Persistence side**: ``BarPersistence._payload_key`` keys on
  ``(end_ms, open, high, low, close, volume)``. A producer that
  changed ``end_ms`` without changing ``start_ms`` would register as
  a *correction*, not a duplicate — making the divergence visible in
  the ``applied_correction`` counter instead of silently absorbed.
* **Consumer side**: ``get_chart_snapshot`` serializes bars via
  ``IbkrMinuteBar.model_dump(mode="json")`` — both fields ride out as
  plain integers, no string coercion, no naive datetime round-trip.

## Regression test

``PythonDataService/tests/services/test_bar_timestamp_rigor.py`` pins
the invariants for both resolutions. It is a *property* test in
spirit: it iterates the supported resolutions, generates a small
deterministic sequence of bars, and asserts the dual-field contract
holds in flight (the model layer) and after a persistence round-trip.

## Ban-list compliance

The Slice 4–6 work introduces no new wire/storage timestamps in any
disallowed shape (no ISO strings, no naive ``datetime``, no
``DateTime.Parse`` on raw strings). All new fields named ``*_ms``,
``ts``, ``date`` are ``int`` or ``YYYY-MM-DD`` *display strings* (the
chart-snapshot envelope's ``date`` field) — never round-tripped back
as wire/storage timestamps.

The ``date`` string on ``ChartSnapshotResponse`` and
``ActiveDateEntry`` is a UTC-date partition key for the operator's
chart picker, not a timestamp. It is never re-parsed into a moment
in time downstream; the server resolves the partition by
``date.fromisoformat(s)`` and uses it solely as a path component.
