"""A failed Clerk background task must not abort the remaining shutdown steps (#3197)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.sqlite import fee_evidence_sync
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper
from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.stream_health_sync import StreamHealthHoldSync


async def _failed() -> None:
    raise RuntimeError("producer failed")


async def _parked() -> None:
    await asyncio.Event().wait()


async def test_fee_evidence_keeps_polling_after_an_unexpected_tick_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sync = FeeEvidenceSync(repo=SimpleNamespace(account_id="paper:test"), read=SimpleNamespace(), intake=SimpleNamespace())
    calls = 0

    async def tick() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected projection failure")
        return False

    async def sleep(_seconds: float) -> None:
        if calls == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(sync, "tick", tick)
    monkeypatch.setattr(fee_evidence_sync, "asyncio", SimpleNamespace(sleep=sleep))

    with pytest.raises(asyncio.CancelledError):
        await sync._run()

    assert calls == 2
    assert any(getattr(record, "action", None) == "clerk_fee_evidence_tick_failed" for record in caplog.records)


@pytest.mark.parametrize("kind", ["fee", "envelope", "stream", "keeper"])
async def test_a_finished_task_error_does_not_escape_stop_or_leave_another_task_running(kind: str) -> None:
    repo = SimpleNamespace(account_id="paper:test")
    failed = asyncio.create_task(_failed())
    await asyncio.wait({failed}, timeout=1)
    parked = asyncio.create_task(_parked())

    if kind == "fee":
        sync = FeeEvidenceSync(repo=repo, read=SimpleNamespace(), intake=SimpleNamespace())
        sync._task = failed
    elif kind == "envelope":
        sync = LiveEnvelopeSync(repo=repo, read=SimpleNamespace(), envelope=SimpleNamespace())
        sync._task = failed
        sync._entry_reading = parked
    elif kind == "stream":
        sync = StreamHealthHoldSync(repo=repo, gate=None)
        sync._task = failed
    else:
        sync = DeadlineKeeper(repo=repo, trade=SimpleNamespace(), intake=SimpleNamespace(), pricing=SimpleNamespace())
        sync._option_events = failed
        sync._task = parked

    try:
        await sync.stop()
        if kind in ("envelope", "keeper"):
            assert parked.cancelled()
    finally:
        parked.cancel()
        await asyncio.gather(parked, return_exceptions=True)
