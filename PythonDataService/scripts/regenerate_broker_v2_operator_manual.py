"""Regenerate the broker-v2 operator manual's generated blocks (ADR 0041).

The manual's **Button Reference** and **Glossary** are produced from the backend
copy map (``app/broker/v2panel/vocabulary.py``), so the documented set *is* the
closed vocabulary. A phantom entry (documented but absent from the enum) becomes
impossible, and a new enum member cannot enter undocumented.

Two files, one artifact (ADR 0041 decision 4):

- ``docs/broker-v2-operator-manual.md`` is the source. Everything outside the
  ``BEGIN GENERATED`` / ``END GENERATED`` markers is hand-written operator prose
  and is preserved verbatim.
- ``Frontend/src/assets/docs/broker-v2-operator-manual.md`` is the copy the app
  serves at ``/brokers/:broker/manual``. It is written as a byte-for-byte copy of
  the source, so the two cannot drift again (they had: the served copy documented
  11 actions, the source 9).

Usage::

    python -m scripts.regenerate_broker_v2_operator_manual

CI regenerates and then runs ``git diff --exit-code`` over both files, the same
shape as the GraphQL schema and OpenAPI contract gates. A hand-edited generated
block, or a hand-edited served copy, fails the build.

Availability is deliberately **not** generated (ADR 0041 decision 2): the backend
does not author it. Enablement is gate logic evaluated per request, and the panel
already renders each blocked action's own runtime reason.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final, get_args

# ``recovery_policy`` pulls in ``app.broker.alpaca`` (broker client, fault
# injection) whose import chain requires ``Settings`` to construct, which in
# turn requires ``POLYGON_API_KEY``. Nothing in this generator calls the
# broker or reads market data; the placeholder makes the import hermetic for
# local runs and CI, matching the same pattern in
# ``scripts/export_openapi_contract.py``.
os.environ.setdefault("POLYGON_API_KEY", "operator-manual-generation-placeholder")

from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryActionId
from app.broker.v2panel.action_policy import ACTION_REGISTRY
from app.broker.v2panel.vocabulary import (
    ACTION_IDS,
    ChannelState,
    DesiredState,
    DutyOutcomeKind,
    HoldReason,
    Phase,
    ReconciliationVerdict,
    StationId,
    StationState,
    copy_for,
    duty_outcome_copy_key,
)

logger = logging.getLogger(__name__)

_REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

CANONICAL_MANUAL_PATH: Final[Path] = _REPOSITORY_ROOT / "docs" / "broker-v2-operator-manual.md"
SERVED_MANUAL_PATH: Final[Path] = (
    _REPOSITORY_ROOT / "Frontend" / "src" / "assets" / "docs" / "broker-v2-operator-manual.md"
)

BUTTON_REFERENCE_BLOCK: Final[str] = "button-reference"
GLOSSARY_BLOCK: Final[str] = "glossary"

REGENERATE_COMMAND: Final[str] = (
    "cd PythonDataService && python -m scripts.regenerate_broker_v2_operator_manual"
)


class ManualGenerationError(RuntimeError):
    """The manual could not be regenerated (missing or malformed markers)."""


def begin_marker(block: str) -> str:
    """Return the opening marker for a generated ``block``."""
    return f"<!-- BEGIN GENERATED: {block} -->"


def end_marker(block: str) -> str:
    """Return the closing marker for a generated ``block``."""
    return f"<!-- END GENERATED: {block} -->"


def _emitted_code_key(code: str) -> str:
    """Most vocabularies key their operator copy by the emitted code itself."""
    return code


# ── Glossary vocabularies ────────────────────────────────────────────────────
# Order comes from each ``Literal`` alias, so the tables follow declaration order
# rather than a hand-kept list. Every section renders the codes the panel
# **emits** and resolves copy through the projection's own key function:
# ``DutyOutcomeKind`` emits ``STOPPED``, whose copy lives under the synthetic key
# ``STOPPED_OUTCOME`` so it cannot collide with the desired-state sense of the
# same word. Dumping copy keys instead would document a code the panel never
# emits, or attach desired-state copy to a duty outcome.
_GLOSSARY_SECTIONS: Final[tuple[tuple[str, tuple[str, ...], Callable[[str], str]], ...]] = (
    ("Phases", get_args(Phase), _emitted_code_key),
    ("Desired State", get_args(DesiredState), _emitted_code_key),
    ("Duty Outcomes", get_args(DutyOutcomeKind), duty_outcome_copy_key),
    ("Hold States", get_args(HoldReason), _emitted_code_key),
    ("Reconciliation Verdicts", get_args(ReconciliationVerdict), _emitted_code_key),
    ("Channel Health", get_args(ChannelState), _emitted_code_key),
    ("Station IDs", get_args(StationId), _emitted_code_key),
    ("Station States", get_args(StationState), _emitted_code_key),
)


def _cell(text: str) -> str:
    """Keep a copy string that contains a pipe from breaking its table row."""
    return text.replace("|", "\\|")


_RECOVERY_ACTION_IDS: Final[frozenset[str]] = frozenset(get_args(RecoveryActionId))


def action_surface(action_id: str) -> str:
    """Return every surface that can present ``action_id``.

    This is the static broker/catalog scope declared in ``ACTION_REGISTRY`` and
    ``RecoveryActionId`` — *not* runtime enablement, which the panel computes per
    request. The two sources are not mutually exclusive: ``reconcile_now`` has a
    registry entry (the legacy/JSONL bot panel) *and* is a member of
    ``RecoveryActionId`` (the SQLite Clerk recovery catalog for activated
    accounts) — both are real, simultaneously available depending on account
    authority mode, so both are listed.

    Two actions (``retire``, ``cancel_order``) carry an empty
    ``supported_brokers`` and are not recovery actions either, so no surface can
    render them; saying so keeps the documented set exactly equal to the enum,
    which omitting them would break.
    """
    policy = ACTION_REGISTRY.get(action_id)
    surfaces: list[str] = []
    if policy is not None and policy.supported_brokers:
        brokers = ", ".join(f"`{broker}`" for broker in sorted(policy.supported_brokers))
        page = "Bots list page" if policy.list_page_only else "Bot panel"
        surfaces.append(f"{page} ({brokers})")
    if action_id in _RECOVERY_ACTION_IDS:
        surfaces.append("SQLite Clerk recovery catalog")
    if not surfaces:
        return "**Nothing — no broker exposes this action.**"
    return " and ".join(surfaces)


def build_button_reference() -> str:
    """Return the Button Reference block body, heading included."""
    rows = "\n".join(
        f"| `{action_id}` | {_cell(copy_for(action_id).label)} "
        f"| {_cell(copy_for(action_id).explanation)} | {action_surface(action_id)} |"
        for action_id in ACTION_IDS
    )
    return f"""## Button Reference {{#button-reference}}

Every action the panel can present, with the label and explanation the backend
authors. This table is generated from `OPERATOR_COPY`, so it is exactly the
closed `ActionId` enum — no more, no less.

**Where did "when available" go?** It was dropped by decision (ADR 0041). The
backend does not author availability: enablement is gate logic evaluated per
request. The panel renders each action's live condition beside the action itself,
under **Active command gates**, with its own reason — *"No attributed exposure
requires a flatten plan."* A condition computed at the moment of asking cannot
rot; a prose condition can, and did. Read the panel for "can I use this now";
read this table for "what does it do". Losing the browsable conditions list is a
real cost, accepted knowingly.

**Surface** is the static broker scope declared in the action registry. It
answers "can this ever appear for me", not "is it enabled right now".

| Code | Button | What it does | Surface |
|---|---|---|---|
{rows}"""


def _glossary_section(heading: str, codes: tuple[str, ...], copy_key: Callable[[str], str]) -> str:
    """Return one ``### heading`` table: one row per code, in declaration order."""
    rows = "\n".join(
        f"| `{code}` | {_cell(copy_for(copy_key(code)).label)} "
        f"| {_cell(copy_for(copy_key(code)).explanation)} |"
        for code in codes
    )
    return f"### {heading}\n\n| Code | Label | Meaning |\n|---|---|---|\n{rows}\n"


def build_glossary() -> str:
    """Return the Glossary block body, heading included."""
    sections = "\n".join(_glossary_section(*section) for section in _GLOSSARY_SECTIONS)
    return f"""## Glossary {{#glossary}}

The panel uses a closed vocabulary. Every code the system emits is defined below,
generated from the same backend copy map the panel itself renders.

{sections}
### Action IDs

The ninth closed vocabulary is `ActionId`. Every one of its codes is documented —
with the surface that can present it — in the
[Button Reference](#button-reference) above, and is not repeated here."""


def _replace_block(text: str, block: str, body: str) -> str:
    """Return ``text`` with ``block``'s marked region replaced by ``body``."""
    begin, end = begin_marker(block), end_marker(block)
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ManualGenerationError(
            f"expected exactly one {begin} and one {end} in the manual; "
            f"found {text.count(begin)} and {text.count(end)}"
        )
    start = text.index(begin) + len(begin)
    stop = text.index(end)
    if stop < start:
        raise ManualGenerationError(f"{end} appears before {begin} in the manual")
    return f"{text[:start]}\n\n{body}\n\n{text[stop:]}"


def render_manual(source: str) -> str:
    """Return ``source`` with both generated blocks refreshed."""
    rendered = _replace_block(source, BUTTON_REFERENCE_BLOCK, build_button_reference())
    return _replace_block(rendered, GLOSSARY_BLOCK, build_glossary())


def expected_manual_text() -> str:
    """Return the manual as it must appear on disk, from the committed source."""
    if not CANONICAL_MANUAL_PATH.is_file():
        raise ManualGenerationError(f"manual source is missing: {CANONICAL_MANUAL_PATH}")
    return render_manual(CANONICAL_MANUAL_PATH.read_text(encoding="utf-8"))


def write_manuals() -> tuple[Path, Path]:
    """Rewrite the source manual and republish the served copy; return both paths."""
    text = expected_manual_text()
    CANONICAL_MANUAL_PATH.write_text(text, encoding="utf-8")
    SERVED_MANUAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERVED_MANUAL_PATH.write_text(text, encoding="utf-8")
    return CANONICAL_MANUAL_PATH, SERVED_MANUAL_PATH


def main() -> int:
    """CLI entry point — refresh the generated blocks and the served copy."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        canonical, served = write_manuals()
    except ManualGenerationError as error:
        logger.error("manual regeneration failed: %s", error)
        logger.error("fix the markers, then re-run: %s", REGENERATE_COMMAND)
        return 1
    logger.info("wrote manual source: %s", canonical)
    logger.info("wrote served manual copy: %s", served)
    return 0


if __name__ == "__main__":
    sys.exit(main())
