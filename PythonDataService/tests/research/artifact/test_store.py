"""Unit tests for ``ArtifactStore`` / ``ArtifactDescriptor``.

Covers the persistence-mechanics surface the seam doc names as the
PR-1 acceptance bar: save/load round-trip, atomic write contract,
path-traversal defence, descriptor-supplied error classes, and
``list_ids`` filtering. Phase-specific behaviour (e.g. the MC
``method`` filter, the runs canonical-JSON hash) is tested at the
phase layer.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from app.research.artifact import (
    ArtifactAlreadyExistsError,
    ArtifactCorruptError,
    ArtifactDescriptor,
    ArtifactNotFoundError,
    ArtifactStore,
)


# ---------------------------------------------------------------------------
# Test fixtures: small generic config / result models and a descriptor.
# ---------------------------------------------------------------------------
class _FixtureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    parent_run_id: str | None = None
    created_at_ms: int
    payload: str = ""


class _FixtureResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    score: float
    created_at_ms: int


class _PhaseNotFound(ArtifactNotFoundError):
    """Phase-named subclass to verify the descriptor-supplied class is raised."""


class _PhaseAlreadyExists(ArtifactAlreadyExistsError):
    """Phase-named subclass for the already-exists path."""


class _PhaseCorrupt(ArtifactCorruptError):
    """Phase-named subclass for the corrupt-load path."""


_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_HASH_TRACK: list[str] = []


def _hash_callback(cfg: BaseModel) -> str:
    """Test spy: records every config that gets hashed and returns a fake digest."""
    _HASH_TRACK.append(getattr(cfg, "artifact_id", ""))
    return "deadbeef"


def _descriptor(*, with_hash: bool = False) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        subdir="phase-x",
        id_field="artifact_id",
        id_pattern=_ID_PATTERN,
        config_filename="config.json",
        result_filename="result.json",
        parent_run_id_extractor=lambda cfg: getattr(cfg, "parent_run_id", None),
        log_tag="PHX",
        hash_payload=_hash_callback if with_hash else None,
        not_found_error=_PhaseNotFound,
        already_exists_error=_PhaseAlreadyExists,
        corrupt_error=_PhaseCorrupt,
    )


def _make_config(**overrides) -> _FixtureConfig:
    base: dict = {
        "artifact_id": "a" * 32,
        "parent_run_id": None,
        "created_at_ms": 1_700_000_000_000,
        "payload": "",
    }
    base.update(overrides)
    return _FixtureConfig(**base)


def _make_result(**overrides) -> _FixtureResult:
    base: dict = {
        "artifact_id": "a" * 32,
        "score": 0.5,
        "created_at_ms": 1_700_000_000_000,
    }
    base.update(overrides)
    return _FixtureResult(**base)


@pytest.fixture(autouse=True)
def _reset_hash_track():
    _HASH_TRACK.clear()
    yield
    _HASH_TRACK.clear()


# ---------------------------------------------------------------------------
# Round-trip with Pydantic types.
# ---------------------------------------------------------------------------
def test_save_load_round_trips(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    config = _make_config(payload="hello")
    result = _make_result(score=1.25)

    artifact_dir = store.save(config, result)

    assert artifact_dir == tmp_path / "phase-x" / config.artifact_id
    assert (artifact_dir / "config.json").is_file()
    assert (artifact_dir / "result.json").is_file()

    loaded_config, loaded_result = store.load(
        config.artifact_id,
        config_type=_FixtureConfig,
        result_type=_FixtureResult,
    )
    assert loaded_config.model_dump() == config.model_dump()
    assert loaded_result.model_dump() == result.model_dump()


# ---------------------------------------------------------------------------
# Path-traversal defence.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_id",
    [
        "../../../etc/passwd",
        "..",
        "/",
        "abc/../def",
        "abc def",
        "ABCDEFABCDEFABCDEFABCDEFABCDEFAB",  # uppercase
        "a" * 31,  # too short
        "a" * 33,  # too long
        "-" * 32,
        "",
    ],
)
def test_load_rejects_traversal_or_malformed_id(tmp_path: Path, bad_id: str):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    with pytest.raises(ValueError):
        store.load(bad_id, config_type=_FixtureConfig, result_type=_FixtureResult)


def test_save_rejects_id_outside_pattern(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    # The config carries an id that fails the descriptor's regex.
    bad_config = _FixtureConfig(
        artifact_id="../escape",
        parent_run_id=None,
        created_at_ms=1_700_000_000_000,
    )
    bad_result = _FixtureResult(
        artifact_id="../escape",
        score=0.0,
        created_at_ms=1_700_000_000_000,
    )
    with pytest.raises(ValueError):
        store.save(bad_config, bad_result)


# ---------------------------------------------------------------------------
# Save semantics.
# ---------------------------------------------------------------------------
def test_save_refuses_overwrite(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    config = _make_config()
    result = _make_result()
    store.save(config, result)

    with pytest.raises(_PhaseAlreadyExists):
        store.save(config, result)


def test_save_replace_clobbers(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    config = _make_config()
    result = _make_result(score=0.1)
    store.save(config, result)

    new_result = _make_result(score=0.9)
    store.save(config, new_result, replace=True)

    _, loaded_result = store.load(
        config.artifact_id,
        config_type=_FixtureConfig,
        result_type=_FixtureResult,
    )
    assert loaded_result.score == 0.9


# ---------------------------------------------------------------------------
# Load failure modes.
# ---------------------------------------------------------------------------
def test_load_missing_raises_descriptor_not_found(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    with pytest.raises(_PhaseNotFound):
        store.load(
            "b" * 32, config_type=_FixtureConfig, result_type=_FixtureResult
        )


def test_load_corrupt_config_raises_descriptor_corrupt(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    config = _make_config()
    result = _make_result()
    store.save(config, result)
    (tmp_path / "phase-x" / config.artifact_id / "config.json").write_text(
        "{not valid json"
    )
    with pytest.raises(_PhaseCorrupt, match=r"config\.json"):
        store.load(
            config.artifact_id,
            config_type=_FixtureConfig,
            result_type=_FixtureResult,
        )


def test_load_corrupt_result_raises_descriptor_corrupt(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    config = _make_config()
    result = _make_result()
    store.save(config, result)
    (tmp_path / "phase-x" / config.artifact_id / "result.json").write_text(
        "{not valid json"
    )
    with pytest.raises(_PhaseCorrupt, match=r"result\.json"):
        store.load(
            config.artifact_id,
            config_type=_FixtureConfig,
            result_type=_FixtureResult,
        )


# ---------------------------------------------------------------------------
# list_ids filtering.
# ---------------------------------------------------------------------------
def test_list_ids_empty_returns_empty(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    assert store.list_ids() == []


def test_list_ids_orders_newest_first(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    older_id = "a" * 32
    newer_id = "b" * 32
    store.save(
        _make_config(artifact_id=older_id, created_at_ms=1_000),
        _make_result(artifact_id=older_id, created_at_ms=1_000),
    )
    store.save(
        _make_config(artifact_id=newer_id, created_at_ms=2_000),
        _make_result(artifact_id=newer_id, created_at_ms=2_000),
    )

    assert store.list_ids() == [newer_id, older_id]


def test_list_ids_filter_by_parent_run_id(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    a_id = "a" * 32
    b_id = "b" * 32
    store.save(
        _make_config(artifact_id=a_id, parent_run_id="parent-1"),
        _make_result(artifact_id=a_id),
    )
    store.save(
        _make_config(artifact_id=b_id, parent_run_id="parent-2"),
        _make_result(artifact_id=b_id),
    )

    assert store.list_ids(parent_run_id="parent-1") == [a_id]
    assert store.list_ids(parent_run_id="parent-2") == [b_id]
    assert store.list_ids(parent_run_id="nope") == []


def test_list_ids_filter_by_since_ms(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    older_id = "a" * 32
    newer_id = "b" * 32
    store.save(
        _make_config(artifact_id=older_id, created_at_ms=1_000),
        _make_result(artifact_id=older_id, created_at_ms=1_000),
    )
    store.save(
        _make_config(artifact_id=newer_id, created_at_ms=2_000),
        _make_result(artifact_id=newer_id, created_at_ms=2_000),
    )

    assert store.list_ids(since_ms=1_500) == [newer_id]
    assert store.list_ids(since_ms=0) == [newer_id, older_id]


def test_list_ids_limit_caps_after_sort(tmp_path: Path):
    store = ArtifactStore(_descriptor(), root=tmp_path)
    ids = [chr(ord("a") + i) * 32 for i in range(3)]
    for i, artifact_id in enumerate(ids):
        store.save(
            _make_config(artifact_id=artifact_id, created_at_ms=1_000 * (i + 1)),
            _make_result(artifact_id=artifact_id, created_at_ms=1_000 * (i + 1)),
        )

    listed = store.list_ids(limit=2)
    # Newest two.
    assert listed == [ids[2], ids[1]]


def test_list_ids_skips_dir_with_corrupt_config(tmp_path: Path, caplog):
    """A properly-named dir whose ``config.json`` won't parse is skipped with a warning.

    Tests the parse-failure code path: the dir name matches
    ``id_pattern`` (so it survives the pre-filter), but the
    ``config.json`` payload is malformed. The store warns and skips
    so a single broken artifact doesn't blind the listing. ``load``
    of the same id would raise loudly; ``list_ids`` is the forgiving
    enumerator.
    """
    store = ArtifactStore(_descriptor(), root=tmp_path)
    good_id = "a" * 32
    store.save(_make_config(artifact_id=good_id), _make_result(artifact_id=good_id))

    # Properly-named dir (matches id_pattern) but malformed config —
    # exercises the parse-fail skip path, not the name-mismatch skip.
    corrupt_id = "b" * 32
    debris = tmp_path / "phase-x" / corrupt_id
    debris.mkdir(parents=True)
    (debris / "config.json").write_text("{not valid json")

    with caplog.at_level(logging.WARNING):
        listed = store.list_ids()

    assert listed == [good_id]
    assert any("skipping corrupt config" in rec.message for rec in caplog.records)


def test_list_ids_skips_dir_whose_name_fails_id_pattern(tmp_path: Path, caplog):
    """A dir whose name doesn't match ``id_pattern`` is skipped with a warning.

    Regression for a bug surfaced in the #352 review: the previous
    implementation returned ``child.name`` unconditionally, so a
    caller running ``for id in list_ids(): load(id)`` would hit a
    ValueError on debris dirs (manual debug, partial recovery, an
    accidental mkdir). The store now pre-filters by ``id_pattern``
    and warns so operators see the debris in logs. The warning
    carries ``skipping corrupt`` so it matches the same operator
    grep pattern as the parse-fail path.
    """
    store = ArtifactStore(_descriptor(), root=tmp_path)
    good_id = "a" * 32
    store.save(_make_config(artifact_id=good_id), _make_result(artifact_id=good_id))

    # Non-matching dir name + a valid-looking config inside, to prove
    # the filter rejects on NAME, not on content.
    debris = tmp_path / "phase-x" / "not-a-valid-id"
    debris.mkdir(parents=True)
    (debris / "config.json").write_text(
        '{"artifact_id":"c","parent_run_id":null,"created_at_ms":1,"payload":""}'
    )

    with caplog.at_level(logging.WARNING):
        listed = store.list_ids()

    assert listed == [good_id]
    assert any(
        "skipping corrupt" in rec.message and "not-a-valid-id" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# hash_payload hook.
# ---------------------------------------------------------------------------
def test_hash_callback_invoked_when_present(tmp_path: Path):
    store = ArtifactStore(_descriptor(with_hash=True), root=tmp_path)
    config = _make_config()
    result = _make_result()
    store.save(config, result)
    assert list(_HASH_TRACK) == [config.artifact_id]


def test_hash_callback_not_invoked_when_absent(tmp_path: Path):
    store = ArtifactStore(_descriptor(with_hash=False), root=tmp_path)
    config = _make_config()
    result = _make_result()
    store.save(config, result)
    assert _HASH_TRACK == []


# ---------------------------------------------------------------------------
# id_field disambiguation (regression for the auto-scan bug).
#
# Pre-fix the store auto-scanned every Pydantic field whose value
# matched ``id_pattern`` and raised ``multiple distinct id-shaped
# fields ...`` when more than one matched. ``MonteCarloConfig`` has
# *two* such fields (``monte_carlo_id`` and ``parent_run_id`` — both
# uuid4 hex digests), so every save through the real phase descriptor
# would crash before writing anything to disk. The fix makes the
# descriptor name the id field explicitly via ``id_field=``.
# ---------------------------------------------------------------------------
class _TwoIdConfig(BaseModel):
    """Config carrying two distinct ``id_pattern``-matching fields."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    sibling_id: str  # also matches ``_ID_PATTERN`` — the bug trigger
    created_at_ms: int


class _TwoIdResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    created_at_ms: int


def test_save_uses_explicit_id_field_when_sibling_field_also_matches_pattern(
    tmp_path: Path,
):
    """Two id-shaped fields no longer trip the store.

    Without the ``id_field`` fix this raised
    ``ValueError: ... multiple distinct id-shaped fields ...`` before
    writing anything to disk.
    """
    descriptor = ArtifactDescriptor(
        subdir="phase-x",
        id_field="artifact_id",
        id_pattern=_ID_PATTERN,
        config_filename="config.json",
        result_filename="result.json",
        parent_run_id_extractor=lambda cfg: None,
        log_tag="PHX",
        not_found_error=_PhaseNotFound,
        already_exists_error=_PhaseAlreadyExists,
        corrupt_error=_PhaseCorrupt,
    )
    store = ArtifactStore(descriptor, root=tmp_path)

    chosen_id = "a" * 32
    sibling_id = "b" * 32  # also matches the regex but is not the artifact id

    config = _TwoIdConfig(
        artifact_id=chosen_id,
        sibling_id=sibling_id,
        created_at_ms=1_700_000_000_000,
    )
    result = _TwoIdResult(artifact_id=chosen_id, created_at_ms=1_700_000_000_000)

    artifact_dir = store.save(config, result)

    # Lives under the chosen id (``artifact_id``), not the sibling.
    assert artifact_dir == tmp_path / "phase-x" / chosen_id
    assert (artifact_dir / "config.json").is_file()
    assert not (tmp_path / "phase-x" / sibling_id).exists()


def test_save_rejects_when_id_field_value_fails_pattern(tmp_path: Path):
    """``id_field`` value still gets the regex gate."""
    descriptor = ArtifactDescriptor(
        subdir="phase-x",
        id_field="artifact_id",
        id_pattern=_ID_PATTERN,
        config_filename="config.json",
        result_filename="result.json",
        parent_run_id_extractor=lambda cfg: None,
        log_tag="PHX",
        not_found_error=_PhaseNotFound,
        already_exists_error=_PhaseAlreadyExists,
        corrupt_error=_PhaseCorrupt,
    )
    store = ArtifactStore(descriptor, root=tmp_path)
    bad_config = _FixtureConfig(
        artifact_id="not-hex-32",
        parent_run_id=None,
        created_at_ms=1_700_000_000_000,
    )
    bad_result = _FixtureResult(
        artifact_id="not-hex-32", score=0.0, created_at_ms=1_700_000_000_000
    )
    with pytest.raises(ValueError, match=r"artifact_id"):
        store.save(bad_config, bad_result)


# ---------------------------------------------------------------------------
# log_tag prefixes warnings (PR-4 Q2 resolved).
# ---------------------------------------------------------------------------
def test_list_ids_corrupt_warning_uses_descriptor_log_tag(
    tmp_path: Path, caplog
):
    """A skipped corrupt config logs with the descriptor's ``[<log_tag>]`` prefix.

    Preserves the operator grep patterns established by the pre-seam
    ``[MC] skipping corrupt monte-carlo config in %s: %s`` log line so
    existing dashboards / alerts that filter on ``[MC]`` keep matching.
    """
    descriptor = ArtifactDescriptor(
        subdir="monte-carlo",
        id_field="artifact_id",
        id_pattern=_ID_PATTERN,
        config_filename="config.json",
        result_filename="result.json",
        parent_run_id_extractor=lambda cfg: None,
        log_tag="MC",
        not_found_error=_PhaseNotFound,
        already_exists_error=_PhaseAlreadyExists,
        corrupt_error=_PhaseCorrupt,
    )
    store = ArtifactStore(descriptor, root=tmp_path)

    debris = tmp_path / "monte-carlo" / "corrupt-debris-dir"
    debris.mkdir(parents=True)
    (debris / "config.json").write_text("{not valid json")

    with caplog.at_level(logging.WARNING):
        listed = store.list_ids()

    assert listed == []
    matching = [rec for rec in caplog.records if rec.message.startswith("[MC]")]
    assert matching, (
        "expected a warning prefixed with [MC]; got messages: "
        f"{[rec.message for rec in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# subdir="" (the runs-phase shape — PR-4 Q3 resolved).
#
# ``runs`` is flat: its artifacts live at ``<root>/<run_id>/`` directly
# rather than under an intermediate ``<root>/runs/`` segment. The
# descriptor's ``subdir: str`` field is correct as typed; the runs
# descriptor will pass ``subdir=""``. Encoded here so reviewers of PR 4
# don't flag the empty string as a typo.
# ---------------------------------------------------------------------------
def test_store_with_empty_subdir_writes_artifact_at_root_directly(tmp_path: Path):
    descriptor = ArtifactDescriptor(
        subdir="",
        id_field="artifact_id",
        id_pattern=_ID_PATTERN,
        config_filename="ledger.json",
        result_filename="result.json",
        parent_run_id_extractor=lambda cfg: getattr(cfg, "parent_run_id", None),
        log_tag="RUNS",
        not_found_error=_PhaseNotFound,
        already_exists_error=_PhaseAlreadyExists,
        corrupt_error=_PhaseCorrupt,
    )
    store = ArtifactStore(descriptor, root=tmp_path)

    config = _make_config(payload="flat")
    result = _make_result(score=2.0)

    artifact_dir = store.save(config, result)

    # No intermediate path segment — the artifact lives directly under
    # ``<root>/<id>/``, matching the runs-phase pre-seam layout.
    assert artifact_dir == tmp_path / config.artifact_id
    assert (artifact_dir / "ledger.json").is_file()
    assert (artifact_dir / "result.json").is_file()

    loaded_config, loaded_result = store.load(
        config.artifact_id,
        config_type=_FixtureConfig,
        result_type=_FixtureResult,
    )
    assert loaded_config.payload == "flat"
    assert loaded_result.score == 2.0

    assert store.list_ids() == [config.artifact_id]


# ---------------------------------------------------------------------------
# Defensive: id validation is full-string, not prefix.
# ---------------------------------------------------------------------------
def test_save_rejects_id_when_pattern_is_unanchored_and_id_has_extra_suffix(
    tmp_path: Path,
):
    """Defence against an unanchored descriptor pattern.

    ``pattern.match`` only anchors at the start, so an unanchored
    ``r"[0-9a-f]{32}"`` would silently accept ``"a"*32 + "/escape"``
    as a valid id. The store now uses ``pattern.fullmatch`` so the
    extra suffix is rejected regardless of how the descriptor's regex
    is written. This is a defence-in-depth fix; the production
    descriptors all use anchored ``^...$`` patterns where match and
    fullmatch behave identically.
    """
    unanchored_descriptor = ArtifactDescriptor(
        subdir="phase-x",
        id_field="artifact_id",
        id_pattern=re.compile(r"[0-9a-f]{32}"),  # deliberately unanchored
        config_filename="config.json",
        result_filename="result.json",
        parent_run_id_extractor=lambda cfg: getattr(cfg, "parent_run_id", None),
        log_tag="PHX",
        not_found_error=_PhaseNotFound,
        already_exists_error=_PhaseAlreadyExists,
        corrupt_error=_PhaseCorrupt,
    )
    store = ArtifactStore(unanchored_descriptor, root=tmp_path)

    # 32-hex prefix + path-shaped suffix would pass pattern.match() but
    # fail pattern.fullmatch(). The store should reject it.
    bad_id = "a" * 32 + "/escape"
    config = _make_config(artifact_id=bad_id)
    result = _make_result(artifact_id=bad_id)

    with pytest.raises(ValueError, match=r"must match"):
        store.save(config, result)
