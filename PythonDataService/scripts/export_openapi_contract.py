"""Export the FastAPI OpenAPI contract used by frontend type generation.

The Python data plane is the authority for direct REST contracts. This command
serializes its OpenAPI document deterministically so CI can reject a Pydantic
contract change until the generated TypeScript types are refreshed as well.

Usage::

    python scripts/export_openapi_contract.py
    python scripts/export_openapi_contract.py --check
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SERVICE_ROOT.parent
DEFAULT_OUTPUT = REPOSITORY_ROOT / "contracts" / "openapi" / "python-data-service.openapi.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="export_openapi_contract",
        description="Export or verify the deterministic PythonDataService OpenAPI contract.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the committed OpenAPI contract differs from the current FastAPI schema.",
    )
    return parser.parse_args(argv)


def _schema_text() -> str:
    """Return the app's OpenAPI document with stable JSON formatting."""

    # Schema export never calls Polygon, but Settings requires a key at import
    # time. A harmless placeholder makes this command hermetic in CI.
    os.environ.setdefault("POLYGON_API_KEY", "contract-schema-placeholder")
    sys.path.insert(0, str(SERVICE_ROOT))

    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def _write_atomically(path: Path, text: str) -> None:
    """Replace the contract only after the complete schema is written."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(text)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _check(path: Path, actual: str) -> int:
    """Report a concise unified diff when a committed schema is stale."""

    if not path.is_file():
        sys.stderr.write(f"OpenAPI contract is missing: {path}\n")
        return 1

    expected = path.read_text(encoding="utf-8")
    if expected == actual:
        return 0

    diff = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile=str(path),
        tofile="current FastAPI OpenAPI schema",
        n=3,
    )
    sys.stderr.writelines(diff)
    sys.stderr.write(
        "Regenerate with: python PythonDataService/scripts/export_openapi_contract.py\n"
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    """Export the current schema or verify the checked-in snapshot."""

    args = _parse_args(argv)
    schema = _schema_text()
    if args.check:
        return _check(args.output, schema)

    _write_atomically(args.output, schema)
    sys.stdout.write(f"Wrote OpenAPI contract to {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
