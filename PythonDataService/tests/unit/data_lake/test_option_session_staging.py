"""Real-file capacity, process death and atomic-publication peak guarantees."""
from __future__ import annotations

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import pytest

from app.data_lake.option_session_staging import OptionSessionStaging, StagingCapacityError


def overhead(base: Path) -> int:
    budget = OptionSessionStaging(base)
    budget.reconcile()
    # This accounts for the implementation's bounded ledger/lock allowance,
    # so the cases below isolate the additional retained-byte capacity.
    import json

    return json.loads(budget.ledger.read_bytes())["used_bytes"]


def test_replacement_reserves_atomic_temp_peak_before_changing_original(tmp_path: Path) -> None:
    budget = OptionSessionStaging(tmp_path, max_bytes=overhead(tmp_path) + 1000)
    target = budget.root / "old-generation" / "retained.zip"
    first = budget.reserve(400)
    try:
        first.write({target: b"a" * 400})
    finally:
        first.close()
    short = budget.reserve(599)
    try:
        with pytest.raises(StagingCapacityError):
            short.write({target: b"b" * 600})
        assert target.read_bytes() == b"a" * 400
    finally:
        short.close()
    exact = budget.reserve(600)
    try:
        exact.write({target: b"b" * 600})
    finally:
        exact.close()
    assert target.read_bytes() == b"b" * 600


def test_old_generations_and_publication_temps_count_after_restart(tmp_path: Path) -> None:
    allowance = overhead(tmp_path)
    files = {
        "option_sessions/SPY/old-listing/call.zip": b"v" * 100,
        "option_sessions/SPY/old-listing/.orphan.tmp": b"t" * 50,
        "option_session_publications/request/worker/attempt_2/orphan.tmp": b"q" * 80,
    }
    for name, raw in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    budget = OptionSessionStaging(tmp_path, max_bytes=allowance + 500)
    with pytest.raises(StagingCapacityError):
        budget.reserve(271)
    reserved = budget.reserve(270)
    reserved.close()
    assert all((tmp_path / name).read_bytes() == raw for name, raw in files.items())


@pytest.mark.parametrize("when", ["before", "after"])
def test_publication_temp_disappearing_during_reconciliation_is_safe(tmp_path: Path, monkeypatch, when: str) -> None:
    allowance = overhead(tmp_path)
    budget = OptionSessionStaging(tmp_path, max_bytes=allowance + 1000)
    temporary = budget.publication_root / "publisher" / "session.zip.tmp"
    temporary.parent.mkdir(parents=True)
    temporary.write_bytes(b"p" * 100)
    original_stat, original_symlink = Path.stat, Path.is_symlink
    def disappearing_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == temporary:
            path.unlink()
            if when == "before":
                return original_stat(path, *args, **kwargs)
        return result
    # Avoid an earlier symlink-probe stat consuming the simulated promotion.
    monkeypatch.setattr(Path, "is_symlink", lambda path: False if path == temporary else original_symlink(path))
    monkeypatch.setattr(Path, "stat", disappearing_stat)
    budget.reconcile()
    assert not temporary.exists()
    budget.reconcile()
    exact = budget.reserve(1000)
    exact.close()


def test_concurrent_collectors_cannot_reserve_the_same_capacity(tmp_path: Path) -> None:
    cap = overhead(tmp_path) + 600
    start, release = threading.Barrier(2), threading.Event()
    outcomes: Queue[str] = Queue()
    def collect() -> None:
        budget = OptionSessionStaging(tmp_path, max_bytes=cap)
        start.wait(timeout=5)
        try:
            reservation = budget.reserve(400)
        except StagingCapacityError:
            outcomes.put("paused")
            return
        try:
            outcomes.put("reserved")
            assert release.wait(5)
        finally:
            reservation.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(collect) for _ in range(2)]
        try:
            assert sorted(outcomes.get(timeout=5) for _ in range(2)) == ["paused", "reserved"]
        finally:
            release.set()
        for job in jobs:
            job.result(timeout=5)


# ``-O`` strips asserts: a reservation must hold its lease lock without one, or the reservation reads as dead.
@pytest.mark.parametrize("interpreter_flags", [(), ("-O",)], ids=["default", "optimized"])
def test_dead_process_reclaims_reservation_but_keeps_its_verified_outcome(
    tmp_path: Path, interpreter_flags: tuple[str, ...]
) -> None:
    cap = overhead(tmp_path) + 650
    child = '''
import sys
from pathlib import Path
from app.data_lake.option_session_staging import OptionSessionStaging
budget = OptionSessionStaging(Path(sys.argv[1]), max_bytes=int(sys.argv[2]))
reservation = budget.reserve(600)
reservation.write({budget.root / "original.zip": b"x" * 200})
sys.stdout.write("ready\\n")
sys.stdout.flush()
sys.stdin.read()
'''
    process = subprocess.Popen([sys.executable, *interpreter_flags, "-c", child, str(tmp_path), str(cap)],
        cwd=Path(__file__).resolve().parents[3], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert process.stdout.readline() == b"ready\n"
        # Reconciliation must retain the live process's remaining reservation.
        with pytest.raises(StagingCapacityError):
            OptionSessionStaging(tmp_path, max_bytes=cap).reserve(100)
        process.kill()
        process.wait(timeout=5)
        recovered = OptionSessionStaging(tmp_path, max_bytes=cap).reserve(450)
        recovered.close()
        assert (tmp_path / "option_sessions/original.zip").read_bytes() == b"x" * 200
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.parametrize("name", ["option_session_budget.json", ".option_session_budget.lock", "option_session_budget_leases"])
def test_budget_metadata_symlink_escape_refuses_before_outside_write(tmp_path: Path, name: str) -> None:
    base, outside = tmp_path / "base", tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"retained")
    (base / name).symlink_to(outside if name.endswith("leases") else sentinel)
    with pytest.raises(ValueError, match="escapes root"):
        OptionSessionStaging(base).reconcile()
    assert list(outside.iterdir()) == [sentinel] and sentinel.read_bytes() == b"retained"


def test_internal_metadata_alias_cannot_delete_retained_outcome(tmp_path: Path) -> None:
    budget = OptionSessionStaging(tmp_path)
    budget.root.mkdir(parents=True)
    original = budget.root / ("." + "a" * 32 + ".lock")
    original.write_bytes(b"retained original outcome")
    (tmp_path / "option_session_budget_leases").symlink_to(budget.root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        OptionSessionStaging(tmp_path).reconcile()
    assert original.read_bytes() == b"retained original outcome"


def test_crashed_ledger_atomic_candidates_are_reclaimed_only_as_bookkeeping(tmp_path: Path) -> None:
    allowance = overhead(tmp_path)
    orphan = tmp_path / ".option_session_budget.json.0123456789abcdef.tmp"
    orphan.write_bytes(b"x" * 32_768)
    budget = OptionSessionStaging(tmp_path, max_bytes=allowance + 100)
    budget.reconcile()
    assert not orphan.exists()
    reservation = budget.reserve(100)
    reservation.close()


def test_unknown_reservation_metadata_is_preserved_and_refuses_more_work(tmp_path: Path) -> None:
    budget = OptionSessionStaging(tmp_path)
    budget.locks.mkdir(parents=True)
    unknown = budget.locks / "keep-original.zip"
    unknown.write_bytes(b"unrecognized preserved bytes")
    with pytest.raises(ValueError, match="metadata"):
        budget.reserve(100)
    assert unknown.read_bytes() == b"unrecognized preserved bytes"


def test_tiny_budget_refuses_before_creating_bookkeeping(tmp_path: Path) -> None:
    target = tmp_path / "no-writes"
    with pytest.raises(ValueError, match="at least"):
        OptionSessionStaging(target, max_bytes=1).reconcile()
    assert not target.exists()
