"""Validation tests for tests/fixtures/golden/manifest.json.

These tests run on every PR (CI job: validate-golden-manifest).
They verify:
1. manifest.json is valid JSON and conforms to manifest.schema.json.
2. Every active fixture's files exist on disk.
3. The Pydantic model round-trips cleanly (serialize → parse → re-serialize).
4. Fixture IDs are unique.
5. active_version matches an entry in the versions dict.
6. content_sha256 and file_sha256 values are 64-char lowercase hex.
7. canonical_module paths exist on disk (detects stale references after file moves).
8. canonical_callable is importable from canonical_module (detects renames).
9. validated_by test files exist and active fixtures have no proof gaps.
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


def test_file_hashes_match_disk() -> None:
    """file_sha256 values in the manifest must match the actual bytes on disk.

    This catches any fixture file that was modified after the manifest was
    written (silent edit, accidental overwrite, or regeneration without a
    manifest update).
    """
    import sys

    sys.path.insert(0, str(FIXTURES_DIR))
    from golden_support.hashing import file_sha256 as compute_file_sha256
    from golden_support.registry import Registry

    reg = Registry(MANIFEST_PATH)
    mismatches: list[str] = []
    for fixture in reg.all():
        if fixture.status in ("planned", "deprecated"):
            continue
        files = fixture.active_files
        if files is None:
            continue
        fixture_dir = reg.fixture_dir(fixture.id)
        for fname, expected in files.file_sha256.items():
            path = fixture_dir / fname
            if not path.exists():
                continue  # already caught by test_active_fixture_files_exist
            actual = compute_file_sha256(path)
            if actual != expected:
                mismatches.append(
                    f"{fixture.id} v{fixture.active_version} {fname!r}: "
                    f"manifest={expected[:16]}…, disk={actual[:16]}…"
                )
    assert not mismatches, (
        "file_sha256 mismatches — fixture files modified after manifest was written:\n"
        + "\n".join(mismatches)
    )


def test_content_hashes_match_disk() -> None:
    """content_sha256 values in the manifest must match the normalized content on disk.

    content_sha256 is the canonical identity of the fixture data (Arrow columns
    sorted, dtypes normalized). This catches logical data changes even if the raw
    file bytes differ due to re-serialization.
    """
    import sys

    sys.path.insert(0, str(FIXTURES_DIR))
    from golden_support.hashing import compute_hashes
    from golden_support.registry import Registry

    reg = Registry(MANIFEST_PATH)
    mismatches: list[str] = []
    for fixture in reg.all():
        if fixture.status in ("planned", "deprecated"):
            continue
        files = fixture.active_files
        if files is None:
            continue
        fixture_dir = reg.fixture_dir(fixture.id)
        # Compute content hashes only for files that exist and are in the manifest dict
        covered = [
            fname
            for fname in files.content_sha256
            if (fixture_dir / fname).exists()
        ]
        if not covered:
            continue
        content_hashes, _ = compute_hashes(fixture_dir, covered)
        for fname, actual in content_hashes.items():
            expected = files.content_sha256[fname]
            if actual != expected:
                mismatches.append(
                    f"{fixture.id} v{fixture.active_version} {fname!r}: "
                    f"manifest={expected[:16]}…, disk={actual[:16]}…"
                )
    assert not mismatches, (
        "content_sha256 mismatches — fixture data changed without a manifest update:\n"
        + "\n".join(mismatches)
    )


def test_attribution_hashes_match_disk() -> None:
    """When attribution files are covered by file_sha256, hashes must match disk.

    attribution.md files are not required to be hashed (legacy fixtures predate
    this enforcement), but any fixture that includes the attribution filename in
    file_sha256 must have a hash that matches the actual file.

    New fixtures should include attribution.md in file_sha256 to prevent silent
    modifications to the audit trail.
    """
    import sys

    sys.path.insert(0, str(FIXTURES_DIR))
    from golden_support.hashing import file_sha256 as compute_file_sha256
    from golden_support.registry import Registry

    reg = Registry(MANIFEST_PATH)
    mismatches: list[str] = []
    for fixture in reg.all():
        if fixture.status in ("planned", "deprecated"):
            continue
        files = fixture.active_files
        if files is None:
            continue
        fixture_dir = reg.fixture_dir(fixture.id)
        attribution_fname = files.attribution
        expected = files.file_sha256.get(attribution_fname)
        if expected is None:
            continue  # not yet hashed — not enforced for legacy fixtures
        path = fixture_dir / attribution_fname
        if not path.exists():
            continue  # already caught by test_active_fixture_files_exist
        actual = compute_file_sha256(path)
        if actual != expected:
            mismatches.append(
                f"{fixture.id} v{fixture.active_version} {attribution_fname!r}: "
                f"manifest={expected[:16]}…, disk={actual[:16]}…"
            )
    assert not mismatches, (
        "attribution file_sha256 mismatches — attribution.md modified after manifest was written:\n"
        + "\n".join(mismatches)
    )


def test_canonical_modules_exist_on_disk() -> None:
    """Every active fixture's canonical_module must resolve to a real .py file.

    For comma-separated module lists (ENG-008), each module is checked
    independently. Planned fixtures are skipped — their canonical_module is
    a placeholder until implementation lands.
    """
    data = _load_manifest_raw()
    # canonical_module paths are relative to the repo root, e.g.
    # "PythonDataService/app/research/validation/ic.py"
    # GOLDEN_DIR = <repo>/PythonDataService/tests/fixtures/golden/
    # So repo_root = GOLDEN_DIR.parent.parent.parent.parent
    repo_root = GOLDEN_DIR.parent.parent.parent.parent
    missing: list[str] = []
    for fixture in data.get("fixtures", []):
        if fixture.get("status") == "planned":
            continue
        fid = fixture["id"]
        modules = fixture["canonical_module"]
        for mod in modules.split(","):
            mod = mod.strip()
            # Strip parenthetical annotations like "(shared base: _rsi_range_base.py)"
            mod = mod.split("(")[0].strip()
            if not mod:
                continue
            path = repo_root / mod
            if not path.exists():
                missing.append(
                    f"{fid}: canonical_module={mod!r} does not exist at {path}"
                )
    assert not missing, (
        "Stale canonical_module references (files moved/deleted without manifest update):\n"
        + "\n".join(missing)
    )


def test_canonical_callables_importable() -> None:
    """Every active fixture's canonical_callable must be importable from canonical_module.

    For comma-separated lists, each callable is checked against its corresponding
    module. Planned fixtures are skipped.

    Supports both top-level callables (``compute_information_coefficient``)
    and class methods (``OptionsFeatures.compute_iv_rank``).

    The PR manifest job installs only the manifest tooling (pydantic,
    jsonschema, pyarrow, pytest), so a canonical module whose *third-party*
    dependency is missing is skipped here rather than failed — that says
    nothing about manifest staleness, and the daily full suite runs this
    same check with every dependency installed. A missing or renamed repo
    module, or a missing callable attribute, is always a failure.
    """
    import importlib
    import sys

    data = _load_manifest_raw()
    # canonical_module paths are relative to the repo root, e.g.
    # "PythonDataService/app/research/validation/ic.py"
    # The importable dotted path strips "PythonDataService/" and ".py",
    # e.g. "app.research.validation.ic"
    svc_root = GOLDEN_DIR.parent.parent.parent  # PythonDataService/
    sys.path.insert(0, str(svc_root))

    repo_top_level = {"app", "scripts", "tests"}
    failures: list[str] = []
    for fixture in data.get("fixtures", []):
        if fixture.get("status") == "planned":
            continue
        fid = fixture["id"]
        modules_str = fixture["canonical_module"]
        callables_str = fixture["canonical_callable"]

        modules = [m.strip().split("(")[0].strip() for m in modules_str.split(",")]
        callables = [c.strip() for c in callables_str.split(",")]

        for mod, callable_name in zip(modules, callables, strict=True):
            # Convert "PythonDataService/app/research/validation/ic.py" to import path
            parts = mod.split("/")
            # Drop the "PythonDataService" prefix (first component) and ".py" suffix
            module_parts = [*parts[1:-1], parts[-1].replace(".py", "")]
            module_dotted = ".".join(module_parts)

            try:
                mod_obj = importlib.import_module(module_dotted)
            except ModuleNotFoundError as e:
                if e.name and e.name.split(".")[0] not in repo_top_level:
                    # Third-party dependency absent from this minimal
                    # environment — defer importability to the daily suite.
                    continue
                failures.append(
                    f"{fid}: cannot import module {module_dotted!r}: {e}"
                )
                continue
            except Exception as e:
                failures.append(
                    f"{fid}: cannot import module {module_dotted!r}: {e}"
                )
                continue

            # callable_name may be "compute_information_coefficient" or
            # "OptionsFeatures.compute_iv_rank" (dotted class.method)
            obj = mod_obj
            for attr in callable_name.split("."):
                if not hasattr(obj, attr):
                    failures.append(
                        f"{fid}: callable {callable_name!r} not found in {module_dotted!r} "
                        f"(attribute {attr!r} missing)"
                    )
                    break
                obj = getattr(obj, attr)

    assert not failures, (
        "canonical_callable import failures (callable renamed or module restructured):\n"
        + "\n".join(failures)
    )


def test_validated_by_test_files_exist() -> None:
    """Every active fixture's validated_by test path must point to a real test file.

    Planned fixtures have validated_by=null and are skipped. Active fixtures
    with validated_by=null but an active status are flagged as proof gaps.

    validated_by paths are relative to the PythonDataService/ directory
    (e.g. "tests/fixtures/test_research_fixtures.py").
    """
    data = _load_manifest_raw()
    # validated_by paths are relative to PythonDataService/, e.g.
    # "tests/fixtures/test_research_fixtures.py"
    # GOLDEN_DIR = <repo>/PythonDataService/tests/fixtures/golden/
    # svc_root = GOLDEN_DIR.parent.parent.parent = PythonDataService/
    svc_root = GOLDEN_DIR.parent.parent.parent
    missing: list[str] = []
    gaps: list[str] = []

    for fixture in data.get("fixtures", []):
        fid = fixture["id"]
        status = fixture.get("status", "active")
        validated_by = fixture.get("validated_by")

        if status == "planned":
            continue

        if validated_by is None:
            gaps.append(
                f"{fid}: status={status} but validated_by is null — "
                f"active fixture has no executing test registered"
            )
            continue

        for test_path in validated_by:
            # test_path is relative to svc_root, e.g. "tests/fixtures/test_research_fixtures.py"
            # Strip any "::TestClassName" suffix for file existence check
            file_part = test_path.split("::")[0]
            path = svc_root / file_part
            if not path.exists():
                missing.append(
                    f"{fid}: validated_by={test_path!r} — file not found at {path}"
                )

    assert not missing, (
        "validated_by references to non-existent test files:\n"
        + "\n".join(missing)
    )
    assert not gaps, (
        "Active fixtures with no validated_by test (proof gaps):\n"
        + "\n".join(gaps)
    )
