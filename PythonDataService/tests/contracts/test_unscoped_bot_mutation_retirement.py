"""Structural contract for the unscoped bot-mutation retirement (#2069).

The fleet's own transport is the *account-scoped* surface: every route here
had no catalog operation resolving to it, no frontend caller (Frontend builds
every mutation URL through fleet/clerk-scoped-url.ts) and no script caller.
"""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_APPLICATION_ROOT = REPOSITORY_ROOT / "Frontend" / "src" / "app"

#: Template-literal spellings of the retired unscoped URLs, as this codebase
#: writes them. See the frontend test's docstring for the concatenation gap.
RETIRED_UNSCOPED_URL_LITERALS = ("}/bots`", "}/bots/${", "/bots/stop")


def test_the_frontend_builds_no_unscoped_bot_mutation_url() -> None:
    """Every Frontend mutation goes through fleet/clerk-scoped-url.ts.

    Matches the template-literal spelling this codebase actually writes. A URL
    assembled by concatenation (``scope + '/bots'``) would pass — a real gap,
    demonstrated by mutation rather than assumed, and stated here rather than
    papered over: no such spelling exists in Frontend/src today, and widening
    the match against a shape that does not occur would trade a named blind
    spot for an unexamined one.
    """
    offenders = sorted(
        f"{path.relative_to(FRONTEND_APPLICATION_ROOT)}: {literal}"
        for path, source in (
            (candidate, candidate.read_text(encoding="utf-8"))
            for candidate in FRONTEND_APPLICATION_ROOT.rglob("*.ts")
            if not candidate.name.endswith(".spec.ts")
            and candidate.name != "broker.types.ts"
        )
        for literal in RETIRED_UNSCOPED_URL_LITERALS
        if literal in source
    )

    assert offenders == [], (
        "the Frontend builds a retired unscoped bot-mutation URL; route it "
        f"through fleet/clerk-scoped-url.ts instead: {offenders}"
    )
