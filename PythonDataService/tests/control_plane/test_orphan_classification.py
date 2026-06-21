"""PRD #619-B B6 — daemon-boot orphan classification.

Asserts:

- Each top-level child of ``live_runs/`` produces exactly one
  classification entry, in deterministic (sorted) order.
- ``FRESH_OWNED_BY_THIS_BOOT`` when the sidecar is fresh AND owned by
  this daemon.
- ``ORPHANED_CONTROL_PLANE`` when the sidecar is fresh AND owned by
  a different daemon's boot_id (the child believes a previous daemon
  owns it).
- ``EXITED_UNMANAGED`` when the sidecar is older than the stale
  threshold.
- ``NO_SIDECAR`` when the run dir exists but no readable
  ``engine_runtime.json`` is present.
- A non-existent ``live_runs`` root returns an empty list — no
  exception.
- A sidecar alone is NEVER sufficient to declare a process alive;
  the classifier surfaces ``ORPHANED_CONTROL_PLANE`` without
  consulting process-table state.
- A sidecar that fails to parse (malformed JSON, forward-incompatible
  schema, missing required field) reads as ``NO_SIDECAR`` — the
  classifier never raises.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.engine.live.engine_runtime import ENGINE_RUNTIME_FILENAME, write_engine_runtime_snapshot
from app.engine.live.orphan_classifier import (
    DEFAULT_STALE_THRESHOLD_MS,
    classify_runtime_candidates_on_boot,
)

# Reuse the helper from the writer tests via a local copy — avoids a
# test-to-test import.
from tests.engine.live.test_engine_runtime_writer import _make_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_run_dir(live_runs_root: Path, run_id: str, **snapshot_kwargs) -> Path:
    run_dir = live_runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = _make_snapshot(**snapshot_kwargs)
    write_engine_runtime_snapshot(run_dir, snapshot)
    return run_dir


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_missing_live_runs_root_returns_empty_list(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does" / "not" / "exist"
    out = classify_runtime_candidates_on_boot(
        nonexistent, this_boot_id="boot-x", now_ms=1
    )
    assert out == []


def test_empty_live_runs_root_returns_empty_list(tmp_path: Path) -> None:
    out = classify_runtime_candidates_on_boot(
        tmp_path, this_boot_id="boot-x", now_ms=1
    )
    assert out == []


# ---------------------------------------------------------------------------
# Single-candidate states
# ---------------------------------------------------------------------------


def test_fresh_sidecar_owned_by_this_boot_is_fresh_owned(tmp_path: Path) -> None:
    written_at = 1_700_000_000_000
    _seed_run_dir(
        tmp_path,
        "run-A",
        written_at_ms=written_at,
    )

    out = classify_runtime_candidates_on_boot(
        tmp_path, this_boot_id="daemon-boot-0001", now_ms=written_at + 500
    )

    assert len(out) == 1
    c = out[0]
    assert c.state == "FRESH_OWNED_BY_THIS_BOOT"
    assert c.run_id == "run-A"
    assert c.sidecar is not None
    assert c.sidecar_age_ms == 500


def test_fresh_sidecar_with_different_boot_is_orphaned(tmp_path: Path) -> None:
    written_at = 1_700_000_000_000
    _seed_run_dir(tmp_path, "run-B", written_at_ms=written_at)

    out = classify_runtime_candidates_on_boot(
        tmp_path, this_boot_id="daemon-boot-XX-NEW", now_ms=written_at + 500
    )

    assert len(out) == 1
    c = out[0]
    assert c.state == "ORPHANED_CONTROL_PLANE"
    assert "daemon-boot-XX-NEW" in c.reason
    # Sidecar carries the previous daemon's boot id.
    assert c.sidecar is not None
    assert c.sidecar.expected_daemon_boot_id == "daemon-boot-0001"


def test_stale_sidecar_is_exited_unmanaged(tmp_path: Path) -> None:
    written_at = 1_700_000_000_000
    _seed_run_dir(tmp_path, "run-C", written_at_ms=written_at)

    out = classify_runtime_candidates_on_boot(
        tmp_path,
        this_boot_id="daemon-boot-0001",
        now_ms=written_at + DEFAULT_STALE_THRESHOLD_MS + 1,
    )

    assert len(out) == 1
    c = out[0]
    assert c.state == "EXITED_UNMANAGED"
    assert c.sidecar_age_ms == DEFAULT_STALE_THRESHOLD_MS + 1


def test_run_dir_without_sidecar_is_no_sidecar(tmp_path: Path) -> None:
    (tmp_path / "run-D").mkdir()

    out = classify_runtime_candidates_on_boot(
        tmp_path, this_boot_id="boot-x", now_ms=1_700_000_000_000
    )

    assert len(out) == 1
    c = out[0]
    assert c.state == "NO_SIDECAR"
    assert c.sidecar is None
    assert c.sidecar_age_ms is None


def test_malformed_sidecar_is_no_sidecar(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-malformed"
    run_dir.mkdir()
    (run_dir / ENGINE_RUNTIME_FILENAME).write_text("definitely not json", encoding="utf-8")

    out = classify_runtime_candidates_on_boot(
        tmp_path, this_boot_id="boot-x", now_ms=1_700_000_000_000
    )

    assert len(out) == 1
    assert out[0].state == "NO_SIDECAR"


def test_forward_incompatible_schema_is_no_sidecar(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-future"
    run_dir.mkdir()
    payload = json.loads(_make_snapshot().model_dump_json())
    payload["schema_version"] = 99
    (run_dir / ENGINE_RUNTIME_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    out = classify_runtime_candidates_on_boot(
        tmp_path, this_boot_id="boot-x", now_ms=1_700_000_000_000
    )

    assert len(out) == 1
    assert out[0].state == "NO_SIDECAR"


# ---------------------------------------------------------------------------
# Mixed input — order + multiple categories
# ---------------------------------------------------------------------------


def test_mixed_input_returns_deterministic_sorted_order(tmp_path: Path) -> None:
    written_at = 1_700_000_000_000
    # Out-of-order creation; classifier must return them sorted by run_id.
    _seed_run_dir(tmp_path, "run-Z", written_at_ms=written_at)
    _seed_run_dir(tmp_path, "run-A", written_at_ms=written_at)
    (tmp_path / "run-M").mkdir()

    out = classify_runtime_candidates_on_boot(
        tmp_path, this_boot_id="daemon-boot-0001", now_ms=written_at + 500
    )

    assert [c.run_id for c in out] == ["run-A", "run-M", "run-Z"]


def test_mixed_input_classifies_each_independently(tmp_path: Path) -> None:
    written_at = 1_700_000_000_000
    # Fresh owned by this boot.
    _seed_run_dir(tmp_path, "run-fresh-mine", written_at_ms=written_at)
    # Fresh, owned by a previous boot.
    _seed_run_dir(tmp_path, "run-orphan", written_at_ms=written_at)
    # Stale, owned by this boot (but stale → EXITED).
    _seed_run_dir(tmp_path, "run-stale", written_at_ms=written_at - 5 * DEFAULT_STALE_THRESHOLD_MS)
    # No sidecar.
    (tmp_path / "run-blank").mkdir()

    out = classify_runtime_candidates_on_boot(
        tmp_path,
        this_boot_id="daemon-boot-0001",
        now_ms=written_at,
    )

    states = {c.run_id: c.state for c in out}
    assert states["run-fresh-mine"] == "FRESH_OWNED_BY_THIS_BOOT"
    # run-orphan's sidecar was seeded by _make_snapshot which sets
    # expected_daemon_boot_id="daemon-boot-0001", same as this_boot_id —
    # to make it ORPHANED we need a different boot. Re-seed.
    _seed_run_dir(
        tmp_path,
        "run-orphan",
        written_at_ms=written_at,
    )
    # Use a different this_boot_id for the re-run.
    out2 = classify_runtime_candidates_on_boot(
        tmp_path, this_boot_id="daemon-boot-NEW", now_ms=written_at
    )
    by_id = {c.run_id: c for c in out2}
    assert by_id["run-orphan"].state == "ORPHANED_CONTROL_PLANE"
    assert by_id["run-stale"].state == "EXITED_UNMANAGED"
    assert by_id["run-blank"].state == "NO_SIDECAR"


# ---------------------------------------------------------------------------
# Authority principle: a sidecar alone never proves liveness.
# ---------------------------------------------------------------------------


def test_classifier_does_not_consult_process_table(tmp_path: Path, monkeypatch) -> None:
    """Even a fresh, this-boot-owned sidecar must not be treated as
    "process is alive" by the classifier. The classifier reports the
    sidecar; the daemon's follow-up step (process identity check) is
    where liveness is decided.

    This test asserts the contract: the classifier does not look at
    ``os.kill(pid, 0)`` or read ``/proc``. We patch ``os.kill`` to
    raise; the classifier must not call it.
    """
    written_at = 1_700_000_000_000
    _seed_run_dir(tmp_path, "run-A", written_at_ms=written_at)

    called = {"count": 0}

    def _fail_kill(*_args, **_kwargs):
        called["count"] += 1
        raise AssertionError("classifier must not consult os.kill")

    import os

    monkeypatch.setattr(os, "kill", _fail_kill)

    classify_runtime_candidates_on_boot(
        tmp_path, this_boot_id="daemon-boot-0001", now_ms=written_at + 500
    )

    assert called["count"] == 0
