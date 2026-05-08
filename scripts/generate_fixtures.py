"""Golden fixture generator CLI.

Usage:
    python scripts/generate_fixtures.py --id BS-001 --justification "Initial BS call price fixture"
    python scripts/generate_fixtures.py --id BS-001 --force --justification "Upgraded py_vollib from 1.0.0 to 1.0.1"
    python scripts/generate_fixtures.py --id BS-001 --dry-run

Rules enforced by this script:
  - Refuses to overwrite an existing version without --force.
  - --force creates a NEW version directory (v2/, v3/, ...) and does NOT
    change active_version in the manifest. Activating the new version is
    a deliberate manifest edit — not automatic.
  - --justification is required for --force runs (explains why regeneration
    was necessary). This message is written to the new attribution.md.
  - --dry-run prints what would be created without touching any file.

This script imports py_vollib (GPL-licensed) for fixture generation.
py_vollib must NOT be imported in app/ — this file is test/generation-only.

Generator registry: add new fixture generators to FIXTURE_GENERATORS below.
Each entry is a callable(version_dir: Path, dry_run: bool) -> None.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
PYTHON_SVC = REPO_ROOT / "PythonDataService"
GOLDEN_DIR = PYTHON_SVC / "tests" / "fixtures" / "golden"
GOLDEN_SUPPORT = PYTHON_SVC / "tests" / "fixtures" / "golden_support"
MANIFEST_PATH = GOLDEN_DIR / "manifest.json"

sys.path.insert(0, str(PYTHON_SVC / "tests" / "fixtures"))
sys.path.insert(0, str(PYTHON_SVC))

# ── Generator imports (registered below) ─────────────────────────────────────
# Each generator lives in its own module under scripts/fixture_generators/.
# Import lazily so missing optional deps don't block the --list flag.


def _lazy_import(module_path: str) -> object:
    import importlib

    return importlib.import_module(module_path)


# ── Manifest helpers ──────────────────────────────────────────────────────────


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _save_manifest(data: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _find_fixture(manifest: dict, fixture_id: str) -> dict | None:
    for f in manifest.get("fixtures", []):
        if f["id"] == fixture_id:
            return f
    return None


def _next_version(fixture: dict | None) -> int:
    if fixture is None:
        return 1
    existing = [int(k) for k in fixture.get("versions", {}).keys()]
    return max(existing, default=0) + 1


def _fixture_dir(fixture_id: str, category: str, version: int) -> Path:
    return GOLDEN_DIR / category / fixture_id / f"v{version}"


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate golden fixtures for the learn-ai test suite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--id", required=True, help="Fixture ID, e.g. BS-001")
    p.add_argument(
        "--force",
        action="store_true",
        help="Create a new version even if one already exists. Does NOT activate it.",
    )
    p.add_argument(
        "--justification",
        default="",
        help="Reason for generation (required with --force).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without touching files.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List available fixture IDs and exit.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.list:
        print("Available fixture generators:")
        for fid in sorted(FIXTURE_GENERATORS):
            print(f"  {fid}")
        return 0

    fixture_id = args.id
    if fixture_id not in FIXTURE_GENERATORS:
        print(f"ERROR: No generator registered for {fixture_id!r}.", file=sys.stderr)
        print(f"Known IDs: {sorted(FIXTURE_GENERATORS)}", file=sys.stderr)
        return 1

    if args.force and not args.justification.strip():
        print(
            "ERROR: --force requires --justification explaining why the fixture is being regenerated.",
            file=sys.stderr,
        )
        return 1

    manifest = _load_manifest()
    existing = _find_fixture(manifest, fixture_id)
    next_ver = _next_version(existing)

    if existing is not None and next_ver > 1 and not args.force:
        print(
            f"ERROR: Fixture {fixture_id!r} version {next_ver - 1} already exists.\n"
            f"  Use --force --justification '<reason>' to create version {next_ver}.\n"
            f"  --force does NOT activate the new version; edit manifest.json manually.",
            file=sys.stderr,
        )
        return 1

    # Determine category from existing manifest entry or generator metadata
    generator = FIXTURE_GENERATORS[fixture_id]
    category = _get_category(fixture_id, manifest)
    version_dir = _fixture_dir(fixture_id, category, next_ver)

    if args.dry_run:
        print(f"[dry-run] Would create: {version_dir}/")
        print(f"[dry-run]   input.arrow, output.arrow, attribution.md")
        if existing is None:
            print(f"[dry-run] Would add manifest entry for {fixture_id!r} with status=planned")
        else:
            print(f"[dry-run] Would add version {next_ver} to existing manifest entry")
            print(f"[dry-run] active_version remains {existing['active_version']} — edit manually to promote")
        return 0

    print(f"Generating {fixture_id!r} version {next_ver} in {version_dir} ...")
    version_dir.mkdir(parents=True, exist_ok=True)
    generator(version_dir=version_dir, justification=args.justification)
    print(f"Done. Files written to {version_dir}")

    if existing is None:
        print(f"  Manifest: no existing entry — fixture must be added to manifest.json manually.")
        print(f"  Run: edit {MANIFEST_PATH}")
    else:
        print(f"  Manifest: version {next_ver} created. active_version={existing['active_version']} unchanged.")
        print(f"  To activate: edit manifest.json and set active_version={next_ver}")

    return 0


def _get_category(fixture_id: str, manifest: dict) -> str:
    """Return the category for a fixture ID, from manifest or ID prefix."""
    existing = _find_fixture(manifest, fixture_id)
    if existing:
        return existing["category"]
    prefix = fixture_id.split("-")[0].lower()
    return {
        "bs": "options-pricing",
        "iv": "options-pricing",
        "svi": "options-pricing",
        "eng": "engine-statistics",
        "ind": "indicators",
    }.get(prefix, "unknown")


# ── Fixture generator registry ────────────────────────────────────────────────
# Key: fixture ID. Value: callable(version_dir, justification) -> None.
# Generators are imported lazily to avoid hard deps when just --list-ing.

def _bs001_generator(version_dir: Path, justification: str = "") -> None:
    from fixture_generators.bs_price import generate_bs001
    generate_bs001(version_dir, justification=justification)


def _bs002_generator(version_dir: Path, justification: str = "") -> None:
    from fixture_generators.bs_price import generate_bs002
    generate_bs002(version_dir, justification=justification)


def _bs003_generator(version_dir: Path, justification: str = "") -> None:
    from fixture_generators.bs_greeks import generate_bs003
    generate_bs003(version_dir, justification=justification)


def _eng001_generator(version_dir: Path, justification: str = "") -> None:
    from fixture_generators.engine_stats import generate_eng001
    generate_eng001(version_dir, justification=justification)


def _eng001b_generator(version_dir: Path, justification: str = "") -> None:
    from fixture_generators.engine_stats import generate_eng001b
    generate_eng001b(version_dir, justification=justification)


FIXTURE_GENERATORS: dict[str, object] = {
    "BS-001": _bs001_generator,
    "BS-002": _bs002_generator,
    "BS-003": _bs003_generator,
    "ENG-001": _eng001_generator,
    "ENG-001b": _eng001b_generator,
}


if __name__ == "__main__":
    sys.exit(main())
