"""PRD #619-D1 — direct tests for the artifact_io canonical helpers.

``read_pydantic_artifact`` was added in 619-B but only exercised via
its downstream callers (engine_runtime, daemon_lease, etc.).
``atomic_write_pydantic_artifact`` is added in 619-D1 as the matching
canonical writer.  Both deserve a focused unit test so the contract
does not have to be re-proven through every caller's test surface.

Three writer concerns are pinned:

1. **Atomicity** — no writer-owned temporary debris on success; a partial
   reader cannot observe a torn intermediate state.
2. **Parent directory autocreation** — callers don't have to mkdir
   defensively before each write.
3. **Byte-stable output** — sorted keys, no whitespace; the on-disk
   shape is deterministic so downstream content-hash or diff
   comparisons are stable across runs.

The reader concerns (missing / OSError / malformed / forward-
incompatible schema) are already exercised by the engine_runtime
writer's tests via ``read_pydantic_artifact`` — repeating them here
would add no signal.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import artifact_io
from app.schemas.artifact_io import (
    atomic_write_pydantic_artifact,
    read_pydantic_artifact,
)


class _Sample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    name: str
    value: int = Field(ge=0)


def test_a_written_artifact_round_trips_leaves_no_tmp_debris_and_is_byte_stable(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"
    artifact = _Sample(name="alpha", value=7)

    atomic_write_pydantic_artifact(path, artifact)
    loaded = read_pydantic_artifact(path, _Sample)
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)

    assert loaded == artifact, "write then read round trips"
    assert path.exists(), "the write publishes the file"
    assert not list(tmp_path.glob(".sample.json.*.tmp")), "no writer-owned tmp debris on success"
    # Stable byte shape matters for downstream content-hash comparisons.
    assert " " not in raw, "serialized output has no spaces"
    assert "\n" not in raw, "serialized output has no newlines"
    # Sorted-keys means schema_version comes before value (alphabetical).
    assert list(parsed.keys()) == sorted(parsed.keys()), "serialized keys are sorted"


def test_write_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deep" / "sample.json"

    atomic_write_pydantic_artifact(path, _Sample(name="alpha", value=1))

    assert path.exists()


def test_write_fsyncs_parent_after_atomic_replace(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "sample.json"
    synced_paths: list[Path] = []
    monkeypatch.setattr(artifact_io, "fsync_parent_dir", synced_paths.append)

    atomic_write_pydantic_artifact(path, _Sample(name="alpha", value=1))

    assert synced_paths == [path]


def test_write_overwrites_existing_file_atomically(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"
    atomic_write_pydantic_artifact(path, _Sample(name="first", value=1))
    atomic_write_pydantic_artifact(path, _Sample(name="second", value=2))

    loaded = read_pydantic_artifact(path, _Sample)
    assert loaded is not None
    assert loaded.name == "second"
    assert loaded.value == 2


def test_write_ignores_leftover_legacy_tmp_file(tmp_path: Path) -> None:
    # A prior writer may have used the old deterministic tmp name.  A new
    # unique-temp write must still publish without sharing that unsafe name.
    path = tmp_path / "sample.json"
    legacy_tmp = tmp_path / "sample.json.tmp"
    legacy_tmp.write_text("debris from a crashed write")

    atomic_write_pydantic_artifact(path, _Sample(name="alpha", value=1))

    assert path.exists()
    assert legacy_tmp.read_text(encoding="utf-8") == "debris from a crashed write"
