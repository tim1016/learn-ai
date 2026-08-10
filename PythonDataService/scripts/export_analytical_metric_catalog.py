"""Generate or verify Strategy Lab's deterministic analytical-metric catalog."""

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
DEFAULT_OUTPUT = REPOSITORY_ROOT / "contracts" / "strategy-lab" / "analytical-metric-catalog-v1.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="export_analytical_metric_catalog",
        description="Export or verify the deterministic Strategy Lab analytical-metric catalog.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Fail when the committed catalog differs.")
    return parser.parse_args(argv)


def catalog_text() -> str:
    """Return the Python-authored catalog as stable, human-reviewable JSON."""

    sys.path.insert(0, str(SERVICE_ROOT))
    from app.research.documentation.analytical_metric_catalog import catalog

    return json.dumps(catalog().model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_atomically(path: Path, text: str) -> None:
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
    if not path.is_file():
        sys.stderr.write(f"Analytical metric catalog is missing: {path}\n")
        return 1
    expected = path.read_text(encoding="utf-8")
    if expected == actual:
        return 0
    sys.stderr.writelines(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=str(path),
            tofile="current analytical metric catalog",
            n=3,
        )
    )
    sys.stderr.write("Regenerate with: python PythonDataService/scripts/export_analytical_metric_catalog.py\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    text = catalog_text()
    if args.check:
        return _check(args.output, text)
    _write_atomically(args.output, text)
    sys.stdout.write(f"Wrote analytical metric catalog to {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
