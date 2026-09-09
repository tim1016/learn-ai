"""``ActiveClerkRuntime``'s background taps: eager start, ordered stop.

Post-wave re-review, ADR 0059 slice 5: a refactor made
``start_background_taps`` start the reconciliation sweep too, so the
periodic pass could race the boot reconciliation pass (both call
``reconcile_once``) and the ADR 0050 revival hook was bound after the loop
had already started. Pinned directly against the runtime's own contract,
with no repository or broker: ``start_background_taps`` starts only the
envelope sync and the hold sync, in that order, and never the sweep;
``close`` stops all three, in ``(envelope_sync, hold_sync, sweep)`` order,
and clears every handle.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime


class _FakeTap:
    """A ``BackgroundSweep`` that records its own ``start``/``stop`` onto a shared log."""

    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    def start(self) -> None:
        self._calls.append(f"{self._name}.start")

    async def stop(self) -> None:
        self._calls.append(f"{self._name}.stop")


@pytest.mark.asyncio
async def test_start_background_taps_starts_envelope_and_hold_sync_but_not_the_sweep() -> None:
    calls: list[str] = []
    envelope_sync = _FakeTap("envelope_sync", calls)
    hold_sync = _FakeTap("hold_sync", calls)
    sweep = _FakeTap("sweep", calls)
    runtime = ActiveClerkRuntime(
        authority_kind="shadow",
        sweep=sweep,
        hold_sync=hold_sync,
        envelope_sync=envelope_sync,
    )

    runtime.start_background_taps()

    assert calls == ["envelope_sync.start", "hold_sync.start"], (
        "start_background_taps must start only the envelope sync and the "
        "hold sync, in that order, and never the reconciliation sweep -- "
        "main.py starts the sweep itself, after boot recovery"
    )

    await runtime.close()

    assert calls == [
        "envelope_sync.start",
        "hold_sync.start",
        "envelope_sync.stop",
        "hold_sync.stop",
        "sweep.stop",
    ], "close() must stop every constructed tap, in (envelope_sync, hold_sync, sweep) order"
    assert runtime.envelope_sync is None
    assert runtime.hold_sync is None
    assert runtime.sweep is None
