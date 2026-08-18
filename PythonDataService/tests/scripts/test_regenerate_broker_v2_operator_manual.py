"""Tests for the broker-v2 operator manual generator (ADR 0041).

Three failure modes are pinned here, matching the ADR's own list of traps:

1. **Two committed manuals drifting** (ADR 0041 decision 4) — the canonical
   source and the Frontend-served copy must be byte-identical on disk, and the
   canonical source must already equal what the generator would produce, so a
   forgotten regeneration step is caught here, not discovered by an operator
   reading a stale page.
2. **A direct dump of ``OPERATOR_COPY`` mis-documenting a synthetic key**
   (ADR 0041 decision 6) — the Glossary must render the *emitted* duty-outcome
   code ``STOPPED``, never the synthetic copy key ``STOPPED_OUTCOME``.
3. **A hand-edited generated block surviving regeneration** — the mechanism the
   CI gate (`regenerate`, then `git diff --exit-code`) depends on: regenerating
   from a corrupted generated block must overwrite the corruption, not preserve
   it.
"""

from __future__ import annotations

from typing import get_args

from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryActionId
from app.broker.v2panel.action_policy import ACTION_REGISTRY
from app.broker.v2panel.vocabulary import ACTION_IDS, copy_for
from scripts.regenerate_broker_v2_operator_manual import (
    CANONICAL_MANUAL_PATH,
    SERVED_MANUAL_PATH,
    ManualGenerationError,
    action_surface,
    begin_marker,
    build_button_reference,
    build_glossary,
    end_marker,
    expected_manual_text,
    render_manual,
)

_MINIMAL_SOURCE = (
    "# Manual\n\n"
    f"{begin_marker('button-reference')}\n{end_marker('button-reference')}\n\n"
    f"{begin_marker('glossary')}\n{end_marker('glossary')}\n"
)


def test_canonical_manual_matches_generator_output() -> None:
    """The committed source is exactly what the generator produces right now.

    This is the local equivalent of the CI gate (regenerate, then
    ``git diff --exit-code``): if this fails, someone edited
    ``vocabulary.py`` or the manual's generated blocks without re-running
    ``scripts.regenerate_broker_v2_operator_manual``.
    """
    on_disk = CANONICAL_MANUAL_PATH.read_text(encoding="utf-8")
    assert on_disk == expected_manual_text()


def test_canonical_and_served_manual_are_identical() -> None:
    """The Frontend-served copy must never drift from the canonical source.

    Regression test for the ADR's own reintroduced failure mode: the served
    copy documented 11 actions while the repo-root copy documented 9. The two
    files are produced by the same write and must stay byte-identical.
    """
    canonical = CANONICAL_MANUAL_PATH.read_text(encoding="utf-8")
    served = SERVED_MANUAL_PATH.read_text(encoding="utf-8")
    assert canonical == served


def test_regeneration_overwrites_a_hand_edited_generated_block() -> None:
    """A hand-edited generated block does not survive regeneration.

    This is the mechanism the CI gate relies on: `git diff --exit-code` only
    catches drift if regenerating from source actually discards a manual edit
    inside the marked region. Corrupt the on-disk Button Reference, regenerate
    from it, and confirm the corruption is gone and the correct row is back.
    """
    corrupted = CANONICAL_MANUAL_PATH.read_text(encoding="utf-8").replace(
        copy_for("deploy").explanation, "HAND-EDITED TEXT THAT SHOULD NOT SURVIVE"
    )
    assert "HAND-EDITED TEXT THAT SHOULD NOT SURVIVE" in corrupted

    regenerated = render_manual(corrupted)

    assert "HAND-EDITED TEXT THAT SHOULD NOT SURVIVE" not in regenerated
    assert copy_for("deploy").explanation in regenerated
    assert regenerated == expected_manual_text()


def test_render_manual_is_idempotent() -> None:
    """Regenerating an already-generated manual produces the same text."""
    once = render_manual(_MINIMAL_SOURCE)
    twice = render_manual(once)
    assert once == twice


def test_render_manual_requires_both_markers() -> None:
    """A manual missing a marker fails loudly instead of silently no-opping."""
    source = "# Manual\n\nno markers here\n"
    try:
        render_manual(source)
    except ManualGenerationError as error:
        assert "button-reference" in str(error)
    else:
        raise AssertionError("expected ManualGenerationError for a markerless manual")


def test_render_manual_rejects_a_duplicated_marker() -> None:
    """Two BEGIN markers for the same block is corruption, not ambiguity to resolve."""
    doubled = _MINIMAL_SOURCE.replace(
        begin_marker("button-reference"),
        f"{begin_marker('button-reference')}\n{begin_marker('button-reference')}",
    )
    try:
        render_manual(doubled)
    except ManualGenerationError:
        pass
    else:
        raise AssertionError("expected ManualGenerationError for a duplicated marker")


def test_button_reference_documents_exactly_the_action_ids() -> None:
    """The generated table's code column is exactly ``ACTION_IDS`` — no more, no less.

    This is the phantom-`start`-cannot-happen guarantee made mechanical: the
    documented set is derived from the enum, so it cannot contain a code
    absent from the enum, and cannot omit one present in it.
    """
    body = build_button_reference()
    documented = {
        line.split("|")[1].strip().strip("`")
        for line in body.splitlines()
        if line.startswith("| `")
    }
    assert documented == set(ACTION_IDS)
    assert "start" not in documented


def test_button_reference_carries_no_when_available_prose() -> None:
    """Decision 2: availability is dropped, not regenerated as worse prose."""
    body = build_button_reference()
    assert "When available" not in body


def test_action_surface_names_the_recovery_catalog_exactly_for_recovery_actions() -> None:
    """``action_surface`` mentions the recovery catalog iff the action is one.

    ``RecoveryActionId`` (``recovery_policy.py``) and ``ACTION_REGISTRY`` are not
    mutually exclusive: ``reconcile_now`` is presented by *both* the legacy bot
    panel and the SQLite Clerk recovery catalog, depending on account authority
    mode. An earlier version of ``action_surface`` treated "has a registry entry"
    and "is a recovery action" as an either/or, which silently dropped
    ``reconcile_now``'s recovery-catalog surface — this test pins the
    compositional, non-exclusive version so that regression can't recur.
    """
    recovery_action_ids = set(get_args(RecoveryActionId))
    for action_id in ACTION_IDS:
        mentions_recovery_catalog = "SQLite Clerk recovery catalog" in action_surface(action_id)
        assert mentions_recovery_catalog == (action_id in recovery_action_ids), action_id


def test_reconcile_now_is_presented_by_both_the_bot_panel_and_the_recovery_catalog() -> None:
    """``reconcile_now`` is dual-homed; the surface column must say so, not pick one."""
    assert "reconcile_now" in ACTION_REGISTRY
    assert "reconcile_now" in get_args(RecoveryActionId)
    surface = action_surface("reconcile_now")
    assert "Bot panel" in surface
    assert "SQLite Clerk recovery catalog" in surface


def test_retire_and_cancel_order_render_as_unsupported_by_any_surface() -> None:
    """Two documented actions have an empty ``supported_brokers`` and are not
    recovery actions either — the generator marks them unsupported rather than
    omitting them, so the documented set stays exactly equal to the enum."""
    for action_id in ("retire", "cancel_order"):
        assert ACTION_REGISTRY[action_id].supported_brokers == frozenset()
        assert action_id not in get_args(RecoveryActionId)
        assert action_surface(action_id) == "**Nothing — no broker exposes this action.**"


def test_glossary_renders_emitted_stopped_not_the_synthetic_copy_key() -> None:
    """Trap 2: the Duty Outcomes table documents the emitted code ``STOPPED``.

    Its copy lives under the synthetic key ``STOPPED_OUTCOME`` in
    ``OPERATOR_COPY`` (vocabulary.py) so it cannot collide with the
    desired-state sense of ``STOPPED``. A direct dump of ``OPERATOR_COPY``
    would document ``STOPPED_OUTCOME`` as if it were an emitted code, or
    attach desired-state copy to the duty outcome. The generator must resolve
    copy through ``duty_outcome_copy_key``, the same helper the panel
    projection uses, and emit the code the panel actually produces.
    """
    body = build_glossary()
    duty_outcomes = body.split("### Duty Outcomes", 1)[1].split("###", 1)[0]

    assert "`STOPPED`" in duty_outcomes
    assert "STOPPED_OUTCOME" not in duty_outcomes
    assert copy_for("STOPPED_OUTCOME").label in duty_outcomes
