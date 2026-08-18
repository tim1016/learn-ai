#!/usr/bin/env python3
"""Mechanical guard for ADR 0039 — an ADR's Status is one closed value on one line.

ADR 0039 Decision 6 pins the form exactly:

    **Status:** <Value>[ YYYY-MM-DD]

with the closed vocabulary ``Accepted`` / ``Proposed`` / ``Superseded`` /
``Retired``, **exactly one occurrence per file**, and **no other line beginning
with ``Status``** (Decision 4: amendment blocks may not carry a Status of their
own).

This is intentionally grep-like, a sibling of ``check_temporal_authority.py``.
It proves the field is a *state*, not that the state is *correct* — under ADR
0039 Decision 1 the value states the decision's standing, which no script can
check.

Fenced code blocks are skipped so an ADR may quote the form it defines; ADR
0039 §6 does exactly that.
"""

from __future__ import annotations

import re
import sys
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

FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Violation:
    path: Path
    line_no: int
    rule: str
    text: str


def status_like_lines(path: Path) -> list[tuple[int, str]]:
    """Every line that claims to be a status, ignoring fenced code blocks."""
    found: list[tuple[int, str]] = []
    in_fence = False
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if STATUS_LABEL.match(raw) or STATUS_PROSE.match(raw):
            found.append((line_no, raw.rstrip()))
    return found


def check_file(path: Path) -> list[Violation]:
    relative = path.relative_to(ROOT)
    found = status_like_lines(path)

    if not found:
        return [
            Violation(
                relative,
                0,
                "missing-status",
                "no `**Status:** <Value>[ YYYY-MM-DD]` line (ADR 0039 Decision 6)",
            )
        ]

    violations = [
        Violation(
            relative,
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
                relative,
                found[0][0],
                "duplicate-status",
                f"{len(found)} Status lines (at {where}); ADR 0039 Decision 4 allows exactly one, in the header",
            )
        )

    return violations


def main() -> int:
    paths = sorted(p for p in ADR_DIR.glob("*.md") if p.is_file())
    if not paths:
        print(f"ADR status guard failed: no ADRs found under {ADR_DIR.relative_to(ROOT)}")
        return 1

    violations = [v for path in paths for v in check_file(path)]
    if not violations:
        print(f"ADR status guard passed ({len(paths)} ADRs).")
        return 0

    print("ADR status guard failed:")
    for violation in violations:
        print(f"{violation.path}:{violation.line_no}: {violation.rule}: {violation.text}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
