"""Regression coverage for durable synthetic-authority activation fences."""

from __future__ import annotations

import multiprocessing
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.synthetic_activation import (
    SyntheticActivationConflict,
    SyntheticActivationInvalid,
    SyntheticActivationRecord,
    SyntheticActivationStore,
)


def _record_payload() -> dict[str, object]:
    return asdict(
        SyntheticActivationRecord.create(
            account_id="sim:ema-1",
            authority_generation=1,
            db_identity_token="sqlite-identity",
            activated_at_ms=1_700_000_000_000,
        )
    )


def _append_in_process(
    artifacts_root: str,
    start: multiprocessing.synchronize.Event,
    results: Any,
) -> None:
    start.wait(timeout=10)
    store = SyntheticActivationStore(Path(artifacts_root))
    record = SyntheticActivationRecord.create(
        account_id="sim:ema-1",
        authority_generation=1,
        db_identity_token="sqlite-identity",
        activated_at_ms=1_700_000_000_000,
    )
    try:
        store.append(record)
    except SyntheticActivationConflict:
        results.put("conflict")
    else:
        results.put("appended")


@pytest.mark.parametrize(
    "payload",
    [
        42,
        ["not", "a", "mapping"],
        "not a mapping",
    ],
)
def test_synthetic_activation_record_rejects_non_mapping_payloads(payload: object) -> None:
    with pytest.raises(SyntheticActivationInvalid, match="invalid shape"):
        SyntheticActivationRecord.from_payload(payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("authority_generation", ["1", True])
def test_synthetic_activation_record_rejects_non_integer_generation(
    authority_generation: object,
) -> None:
    payload = _record_payload()
    payload["authority_generation"] = authority_generation

    with pytest.raises(SyntheticActivationInvalid, match="invalid integer facts"):
        SyntheticActivationRecord.from_payload(payload)


@pytest.mark.parametrize("activated_at_ms", [2**63, -(2**63) - 1])
def test_synthetic_activation_record_rejects_out_of_range_timestamp(activated_at_ms: int) -> None:
    payload = _record_payload()
    payload["activated_at_ms"] = activated_at_ms

    with pytest.raises(SyntheticActivationInvalid, match="signed int64"):
        SyntheticActivationRecord.from_payload(payload)


def test_synthetic_activation_append_serializes_generation_conflicts_across_processes(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_append_in_process, args=(str(tmp_path), start, results))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = sorted(results.get(timeout=15) for _ in workers)
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    assert outcomes == ["appended", "conflict"]
    assert len(SyntheticActivationStore(tmp_path).path.read_text(encoding="utf-8").splitlines()) == 1
