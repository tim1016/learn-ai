"""Contract: `restart.sh` decides Compose ownership by label, never by a name list.

`restart.sh` does two opposite things to containers stuck in ``Created``: it
``rm -f``'s the ones Compose does not own (abandoned ``sleep 1`` probes), and it
``podman start``'s the ones Compose does own (dependents abandoned when a
``depends_on: service_healthy`` gate is slow).

Both halves originally classified by a hardcoded five-name allowlist. That list
was complete when written and is *unknowably* incomplete now: `compose.yaml`
declares exactly those five ``container_name`` values, while the broker clerk
agents (``alpaca-live-clerk``, ``alpaca-paper-clerk``) are contributed by an
overlay. A clerk left in ``Created`` therefore fell on the wrong side of both
tests — reaped as an orphan, and never restarted — destroying a live execution
lane on a routine ``./restart.sh``.

The fix is to ask Compose, via the ``com.docker.compose.project`` label it
stamps on everything it owns. These tests fail if a name list comes back.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RESTART_SCRIPT = REPOSITORY_ROOT / "restart.sh"
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"

_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"


def _script() -> str:
    return RESTART_SCRIPT.read_text(encoding="utf-8")


def _declared_container_names() -> set[str]:
    """The ``container_name`` values `compose.yaml` declares on its own."""
    return set(
        re.findall(
            r"^\s*container_name:\s*(\S+)\s*$",
            COMPOSE_FILE.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )


def test_created_container_triage_filters_on_the_compose_project_label() -> None:
    """Both halves of the Created triage must classify by label."""
    script = _script()

    assert _COMPOSE_PROJECT_LABEL in script, (
        f"restart.sh must decide Compose ownership using the "
        f"{_COMPOSE_PROJECT_LABEL} label that Compose stamps on its own "
        f"containers. Without it, any service not named in the script is "
        f"misclassified — which is how a broker clerk agent got destroyed."
    )

    label_filters = script.count(f"label=${{COMPOSE_LABEL}}")
    assert label_filters >= 2, (
        "Both the orphan-reap and the stuck-restart queries must filter on the "
        f"Compose label; found {label_filters} such filter(s)."
    )


def test_no_hardcoded_container_allowlist_survives() -> None:
    """A reintroduced name list is the regression this guards against.

    Any hardcoded alternation over `compose.yaml`'s declared container names is
    exactly the shape of the original bug: it cannot see services an overlay
    contributes, and overlay-contributed services are precisely the clerk
    agents that carry live execution.
    """
    script = _script()
    declared = _declared_container_names()
    assert declared, "compose.yaml declared no container_name values to check against."

    for alternation in re.findall(r"\^\(([^)]*)\)\$", script):
        names = {candidate for candidate in alternation.split("|") if candidate}
        overlap = names & declared
        assert not overlap, (
            "restart.sh hardcodes a container-name allowlist containing "
            f"{sorted(overlap)}. Classify by the {_COMPOSE_PROJECT_LABEL} label "
            "instead — a name list silently excludes every service an overlay "
            "adds, including the broker clerk agents."
        )
