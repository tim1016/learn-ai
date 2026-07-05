#!/usr/bin/env python3
"""Mechanical guard for ADR 0022 temporal-authority rules.

This is intentionally grep-like. It is not a proof of total compliance; it
catches the patterns most likely to reintroduce the old split authorities.
Every allowlist below names the reason the pattern is permitted.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_CALENDAR = Path("PythonDataService/app/lean_sidecar/trading_calendar.py")

SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "bin",
    "coverage",
    "dist",
    "node_modules",
    "obj",
}


@dataclass(frozen=True)
class Violation:
    path: Path
    line_no: int
    rule: str
    text: str


@dataclass(frozen=True)
class Allow:
    prefix: str
    reason: str

    def matches(self, path: Path) -> bool:
        return path.as_posix().startswith(self.prefix)


DATEPIPE_ALLOWS = [
    Allow(
        "Frontend/src/app/components/portfolio/",
        "Legacy portfolio surface still uses pre-ADR DateTime/string models; migrate with the portfolio storage slice.",
    ),
]

TIMESTAMP_STRING_TYPE_ALLOWS = [
    Allow(
        "Frontend/src/app/graphql/portfolio-types.ts",
        "Legacy portfolio GraphQL model remains on DateTime/string storage until the portfolio storage migration.",
    ),
    Allow(
        "Frontend/src/app/services/data-lab-session.service.ts",
        "Legacy Data Lab session metadata remains on DateTime/string storage until the Data Lab storage migration.",
    ),
    Allow(
        "Frontend/src/app/components/data-lab/",
        "Legacy Data Lab session display remains on DateTime/string storage until the Data Lab storage migration.",
    ),
    Allow(
        "Frontend/src/app/services/golden-fixtures.types.ts",
        "Golden-manifest metadata is an external artifact timestamp, not a trading timestamp wire contract.",
    ),
    Allow(
        "Frontend/src/app/models/market-monitor.ts",
        "Live-vendor liveness payload is an external API boundary; canonicalization belongs at ingestion.",
    ),
]


def iter_files(paths: Iterable[Path], suffixes: tuple[str, ...]) -> Iterable[Path]:
    for base in paths:
        if base.is_file() and base.suffix in suffixes:
            yield base
            continue
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_file() and path.suffix in suffixes:
                yield path


def rel(path: Path) -> Path:
    return path.relative_to(ROOT)


def is_allowed(path: Path, allows: Iterable[Allow]) -> bool:
    return any(allow.matches(path) for allow in allows)


def scan_lines(path: Path, checks: Iterable[tuple[str, re.Pattern[str]]]) -> list[Violation]:
    out: list[Violation] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    for line_no, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in checks:
            if pattern.search(line):
                out.append(Violation(rel(path), line_no, rule, line.strip()))
    return out


def check_calendar_constructor() -> list[Violation]:
    checks = [
        ("calendar-constructor", re.compile(r"\bmcal\.get_calendar\b")),
        ("calendar-import", re.compile(r"\bimport\s+pandas_market_calendars\b")),
    ]
    violations: list[Violation] = []
    files = iter_files([ROOT / "PythonDataService/app", *ROOT.glob("PythonDataService/*.py")], (".py",))
    for path in files:
        if rel(path) == CANONICAL_CALENDAR:
            continue
        violations.extend(scan_lines(path, checks))
    return violations


def check_hardcoded_rth() -> list[Violation]:
    checks = [
        ("hardcoded-rth-open", re.compile(r"\btime\(\s*9\s*,\s*30\s*\)")),
        ("hardcoded-rth-close", re.compile(r"\btime\(\s*16\s*,\s*0\s*\)")),
        ("hardcoded-rth-open-minute", re.compile(r"\b9\s*\*\s*60\s*\+\s*30\b")),
    ]
    violations: list[Violation] = []
    files = iter_files([ROOT / "PythonDataService/app", *ROOT.glob("PythonDataService/*.py")], (".py",))
    for path in files:
        relative = rel(path)
        if relative == CANONICAL_CALENDAR:
            continue
        if relative.as_posix().startswith("PythonDataService/app/engine/tests/"):
            continue
        violations.extend(scan_lines(path, checks))
    return violations


def check_naive_time_helpers() -> list[Violation]:
    checks = [
        ("python-datetime-utcnow", re.compile(r"\bdatetime\.utcnow\s*\(")),
        ("python-datetime-utcfromtimestamp", re.compile(r"\bdatetime\.utcfromtimestamp\s*\(")),
        ("python-datetime-now-without-tz", re.compile(r"\bdatetime\.now\s*\(\s*\)")),
        ("dotnet-datetime-parse", re.compile(r"\bDateTime\.Parse\s*\(")),
    ]
    files = iter_files([ROOT / "PythonDataService/app", ROOT / "Backend"], (".py", ".cs"))
    return [v for path in files for v in scan_lines(path, checks)]


def check_frontend_datepipe() -> list[Violation]:
    checks = [
        ("frontend-datepipe-import", re.compile(r"\bDatePipe\b")),
        ("frontend-datepipe-template", re.compile(r"\|\s*date\s*:")),
    ]
    violations: list[Violation] = []
    for path in iter_files([ROOT / "Frontend/src/app"], (".ts", ".html")):
        relative = rel(path)
        if is_allowed(relative, DATEPIPE_ALLOWS):
            continue
        violations.extend(scan_lines(path, checks))
    return violations


def check_frontend_timestamp_string_types() -> list[Violation]:
    checks = [
        (
            "frontend-timestamp-string-type",
            re.compile(r"\b(?:timestamp|Timestamp|createdAt|updatedAt|executedAt|entryTimestamp|exitTimestamp|startedAt|completedAt|openedAt|closedAt|executionTimestamp|lastTriggered)\??:\s*string\b"),
        ),
    ]
    violations: list[Violation] = []
    for path in iter_files([ROOT / "Frontend/src/app"], (".ts",)):
        relative = rel(path)
        if is_allowed(relative, TIMESTAMP_STRING_TYPE_ALLOWS):
            continue
        violations.extend(scan_lines(path, checks))
    return violations


def main() -> int:
    violations = [
        *check_calendar_constructor(),
        *check_hardcoded_rth(),
        *check_naive_time_helpers(),
        *check_frontend_datepipe(),
        *check_frontend_timestamp_string_types(),
    ]
    if not violations:
        print("Temporal authority guard passed.")
        return 0

    print("Temporal authority guard failed:")
    for violation in violations:
        print(f"{violation.path}:{violation.line_no}: {violation.rule}: {violation.text}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
