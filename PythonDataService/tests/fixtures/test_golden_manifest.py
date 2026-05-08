"""Validation tests for tests/fixtures/golden/manifest.json.

These tests run on every PR (CI job: validate-golden-manifest).
They verify:
1. manifest.json is valid JSON and conforms to manifest.schema.json.
2. Every active fixture's files exist on disk.
3. The Pydantic model round-trips cleanly (serialize → parse → re-serialize).
4. Fixture IDs are unique.
5. active_version matches an entry in the versions dict.
6. content_sha256 and file_sha256 values are 64-char lowercase hex.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

FIXTURES_DIR = Path(__file__).parent
GOLDEN_DIR = FIXTURES_DIR / "golden"
MANIFEST_PATH = GOLDEN_DIR / "manifest.json"
SCHEMA_PATH = GOLDEN_DIR / "manifest.schema.json"

GOLDEN_SUPPORT = FIXTURES_DIR / "golden_support"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_manifest_raw() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_manifest_file_exists() -> None:
    assert MANIFEST_PATH.exists(), f"manifest.json not found at {MANIFEST_PATH}"


def test_schema_file_exists() -> None:
    assert SCHEMA_PATH.exists(), f"manifest.schema.json not found at {SCHEMA_PATH}"


def test_manifest_is_valid_json() -> None:
    raw = MANIFEST_PATH.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert isinstance(data, dict), "manifest.json must be a JSON object"


def test_manifest_conforms_to_json_schema() -> None:
    """Validate manifest.json against the committed JSON schema."""
    data = _load_manifest_raw()
    schema = _load_schema()
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as exc:
        pytest.fail(f"manifest.json fails schema validation: {exc.message}\nPath: {list(exc.path)}")


def test_manifest_pydantic_roundtrip() -> None:
    """Pydantic parse → serialize → re-parse must be stable."""
    import sys

    sys.path.insert(0, str(FIXTURES_DIR))
    from golden_support.manifest import Manifest

    m1 = Manifest.load(MANIFEST_PATH)
    serialized = m1.model_dump_json(indent=2)
    m2 = Manifest.model_validate_json(serialized)
    assert m1.schema_version == m2.schema_version
    assert len(m1.fixtures) == len(m2.fixtures)
    for f1, f2 in zip(m1.fixtures, m2.fixtures, strict=True):
        assert f1.id == f2.id
        assert f1.active_version == f2.active_version


def test_fixture_ids_are_unique() -> None:
    data = _load_manifest_raw()
    ids = [f["id"] for f in data.get("fixtures", [])]
    seen: set[str] = set()
    duplicates: list[str] = []
    for fid in ids:
        if fid in seen:
            duplicates.append(fid)
        seen.add(fid)
    assert not duplicates, f"Duplicate fixture IDs: {duplicates}"


def test_active_version_in_versions() -> None:
    """Every non-planned fixture must have active_version in its versions dict."""
    data = _load_manifest_raw()
    for fixture in data.get("fixtures", []):
        if fixture.get("status") == "planned":
            continue
        fid = fixture["id"]
        active = fixture["active_version"]
        versions = fixture.get("versions", {})
        assert str(active) in versions or active in versions, (
            f"Fixture {fid!r}: active_version={active} not found in versions "
            f"{list(versions.keys())}"
        )


def test_active_fixture_files_exist() -> None:
    """Every active fixture's referenced files must exist on disk."""
    import sys

    sys.path.insert(0, str(FIXTURES_DIR))
    from golden_support.registry import Registry

    reg = Registry(MANIFEST_PATH)
    missing: list[str] = []
    for fixture in reg.all():
        if fixture.status in ("planned", "deprecated"):
            continue
        files = fixture.active_files
        if files is None:
            missing.append(f"{fixture.id}: no FixtureFiles for active_version={fixture.active_version}")
            continue
        fixture_dir = reg.fixture_dir(fixture.id)
        for key in ("input", "output", "attribution"):
            fname = getattr(files, key)
            path = fixture_dir / fname
            if not path.exists():
                missing.append(f"{fixture.id}: {key}={fname!r} not found at {path}")
    assert not missing, "Missing fixture files:\n" + "\n".join(missing)


def test_hash_fields_are_valid_hex() -> None:
    """content_sha256 and file_sha256 must be 64-char lowercase hex."""
    data = _load_manifest_raw()
    bad: list[str] = []
    for fixture in data.get("fixtures", []):
        fid = fixture["id"]
        for version_key, files in fixture.get("versions", {}).items():
            for hash_field in ("content_sha256", "file_sha256"):
                for fname, h in files.get(hash_field, {}).items():
                    if len(h) != 64 or not all(c in "0123456789abcdef" for c in h):
                        bad.append(
                            f"{fid} v{version_key} {hash_field}[{fname!r}]: "
                            f"expected 64-char lowercase hex, got {h!r}"
                        )
    assert not bad, "Invalid hash values:\n" + "\n".join(bad)


def test_tolerance_note_is_non_empty() -> None:
    """Every fixture's tolerance must have a non-empty note."""
    data = _load_manifest_raw()
    bad: list[str] = []
    for fixture in data.get("fixtures", []):
        tol = fixture.get("tolerance", {})
        note = tol.get("note", "").strip()
        if not note:
            bad.append(fixture["id"])
    assert not bad, f"Fixtures with empty tolerance note: {bad}"


def test_schema_version_is_1() -> None:
    data = _load_manifest_raw()
    assert data.get("schema_version") == 1, (
        f"Expected schema_version=1, got {data.get('schema_version')}. "
        "Bump this test if the schema version intentionally advances."
    )
