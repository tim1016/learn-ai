#!/usr/bin/env python3
"""Mechanical guard for ADR 0039 — an ADR's Status is one closed value on one line.

ADR 0039 Decision 6 pins the form exactly:

    **Status:** <Value>[ YYYY-MM-DD]

with the closed vocabulary ``Accepted`` / ``Proposed`` / ``Superseded`` /
``Retired``, **exactly one occurrence per file**, and **no other line beginning
with ``Status``** (Decision 4: amendment blocks may not carry a Status of their
own).

ADR 0040 Decision 4 adds a second, forward-only obligation: every ADR
numbered 0040 or higher that reaches ``Accepted`` must carry exactly one
``**Vocabulary:**`` line, either naming the ``CONTEXT.md`` section it owes or
stating that none is owed. The author decides *whether* vocabulary is owed;
this guard checks only that the line exists, is non-empty, and is not
duplicated. Existing ADRs (0039 and earlier) are not back-filled.

This is intentionally grep-like, a sibling of ``check_temporal_authority.py``.
It proves the fields are *present*, not that their content is *correct* —
under ADR 0039 Decision 1 the Status value states the decision's standing,
and under ADR 0040 Decision 4 the Vocabulary target is an author judgment;
neither is a predicate a script can check.

Fenced code blocks are skipped so an ADR may quote the form it defines; ADR
0039 §6 does exactly that.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = ROOT / "docs/architecture/adrs"

# The canonical form, pinned by ADR 0039 Decision 6.
STATUS_LINE = re.compile(
    r"^\*\*Status:\*\* (Accepted|Proposed|Superseded|Retired)( \d{4}-\d{2}-\d{2})?$"
)

# A metadata label that claims to be a status: `**Status:**`, `- **Status:**`,
# `**Status**:`, `Status:`, and friends.
STATUS_LABEL = re.compile(r"^(?:[-*+] )?\*{0,2}Status\*{0,2}\s*:")

# Decision 6's second clause: no *other* line may begin with `Status`.
STATUS_PROSE = re.compile(r"^Status\b")

# A metadata label that claims to be a Vocabulary declaration: `**Vocabulary:**`,
# `- **Vocabulary:**`, and friends. The bold markers wrap through the colon
# (`**Vocabulary:**`, matching ADR 0039's own `**Status:**` convention), so the
# `\*{0,2}` groups are tried both before and after the mandatory colon — either
# position may hold the closing asterisks depending on house style. Group 1
# captures whatever follows — the target (or "none owed" reason) always starts
# there in every existing declaration (ADRs 0036-0038, 0040, 0041).
VOCABULARY_LABEL = re.compile(r"^(?:[-*+] )?\*{0,2}Vocabulary\*{0,2}:\*{0,2}\s*(.*)$")

# ADR 0040 Decision 4 is forward-only: only ADRs numbered this or higher owe a
# Vocabulary line, and only once they reach Accepted.
VOCABULARY_FORWARD_FROM = 40
ADR_NUMBER = re.compile(r"^(\d+)-")

FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Violation:
    path: Path
    line_no: int
    rule: str
    text: str


def _display_path(path: Path) -> Path:
    """Path relative to the repo root when possible, else as given.

    Falls back for synthetic fixtures used by the regression test, which live
    outside ``ROOT`` on purpose so they can't be mistaken for real ADRs.
    """
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def _scan_lines(path: Path, matches: Callable[[str], bool]) -> list[tuple[int, str]]:
    """Every line satisfying ``matches(raw) -> bool``, ignoring fenced code blocks."""
    found: list[tuple[int, str]] = []
    in_fence = False
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if matches(raw):
            found.append((line_no, raw.rstrip()))
    return found


def status_like_lines(path: Path) -> list[tuple[int, str]]:
    """Every line that claims to be a status, ignoring fenced code blocks."""
    return _scan_lines(path, lambda raw: bool(STATUS_LABEL.match(raw) or STATUS_PROSE.match(raw)))


def vocabulary_like_lines(path: Path) -> list[tuple[int, str]]:
    """Every line that claims to be a Vocabulary declaration, ignoring fenced code blocks."""
    return _scan_lines(path, lambda raw: bool(VOCABULARY_LABEL.match(raw)))


def _check_vocabulary(path: Path, display: Path, status_value: str | None) -> list[Violation]:
    """ADR 0040 Decision 4: exactly one non-empty Vocabulary line, Accepted + 0040 onward only."""
    if status_value != "Accepted":
        return []

    number_match = ADR_NUMBER.match(path.stem)
    if not number_match or int(number_match.group(1)) < VOCABULARY_FORWARD_FROM:
        return []

    found = vocabulary_like_lines(path)
    if not found:
        return [
            Violation(
                display,
                0,
                "missing-vocabulary",
                "no `**Vocabulary:** <CONTEXT.md target>` or `**Vocabulary:** none owed — <reason>` "
                "line (ADR 0040 Decision 4)",
            )
        ]

    violations = [
        Violation(
            display,
            line_no,
            "empty-vocabulary",
            f"{text}  <- Vocabulary line has no target and no \"none owed\" reason",
        )
        for line_no, text in found
        if not VOCABULARY_LABEL.match(text).group(1).strip()
    ]

    if len(found) > 1:
        where = ", ".join(str(line_no) for line_no, _ in found)
        violations.append(
            Violation(
                display,
                found[0][0],
                "duplicate-vocabulary",
                f"{len(found)} Vocabulary lines (at {where}); ADR 0040 Decision 4 allows exactly one",
            )
        )

    return violations


def check_file(path: Path) -> list[Violation]:
    display = _display_path(path)
    found = status_like_lines(path)

    if not found:
        return [
            Violation(
                display,
                0,
                "missing-status",
                "no `**Status:** <Value>[ YYYY-MM-DD]` line (ADR 0039 Decision 6)",
            )
        ]

    violations = [
        Violation(
            display,
            line_no,
            "malformed-status",
            f"{text}  <- expected `**Status:** <Accepted|Proposed|Superseded|Retired>[ YYYY-MM-DD]`",
        )
        for line_no, text in found
        if not STATUS_LINE.match(text)
    ]

    if len(found) > 1:
        where = ", ".join(str(line_no) for line_no, _ in found)
        violations.append(
            Violation(
                display,
                found[0][0],
                "duplicate-status",
                f"{len(found)} Status lines (at {where}); ADR 0039 Decision 4 allows exactly one, in the header",
            )
        )

    status_value = None
    if len(found) == 1:
        status_match = STATUS_LINE.match(found[0][1])
        if status_match:
            status_value = status_match.group(1)

    violations.extend(_check_vocabulary(path, display, status_value))
    return violations


def check_all(paths: list[Path]) -> list[Violation]:
    return [v for path in paths for v in check_file(path)]


def main() -> int:
    paths = sorted(p for p in ADR_DIR.glob("*.md") if p.is_file())
    if not paths:
        print(f"ADR status guard failed: no ADRs found under {ADR_DIR.relative_to(ROOT)}")
        return 1

    violations = check_all(paths)
    if not violations:
        print(f"ADR status guard passed ({len(paths)} ADRs).")
        return 0

    print("ADR status guard failed:")
    for violation in violations:
        print(f"{violation.path}:{violation.line_no}: {violation.rule}: {violation.text}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
