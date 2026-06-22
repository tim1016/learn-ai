"""Truthfulness property test for broker-activity rows (ADR 0014 §3).

The Codex review on PR #645 asked for a property test that locks the
truthfulness contract structurally:

    For every authored row in a recorded corpus,
        render(row.template_key, row.template_version, facts_recovered_from_row)
        == (row.headline, row.narrative)

The property this enforces is stronger than the per-scenario assertions
in ``test_broker_activity_reconciler.py``: those say "for THIS scenario,
authoring produces THIS row." This file says "every row's stored typed
fields are sufficient to reproduce its rendered strings, with no hidden
data dependency beyond the publisher's known timing policy."

Why it matters:

- A template that silently references a hidden engine field (anything
  not on the row) would let the authored string drift from what an
  auditor reading the row alone could verify. This test fails the
  moment that happens.
- The row's stored fields ARE the audit replay surface. If they are
  insufficient to reproduce ``headline``/``narrative``, the WAL is
  lying about what the operator saw.

Corpus: a constructive corpus built from the reason→template map. Every
``(template_key, template_version)`` in the registry must appear in the
corpus, or the registry has a template the reconciler can never select
(an unreachable rendering) and the test fails closed.

The reconstruction helper ``_facts_from_row`` is the formal inverse of
``_facts_for_template``: it reads only ``BrokerActivityRow`` fields and
the publisher's known ``ReconciliationTimingPolicy`` (a per-instance
config the publisher keeps). If a future template needs a fact derivable
from neither, the test will fail at the reconstruction step, surfacing
the contract break before it ships.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from app.broker.ibkr.models import IbkrOrderEvent
from app.schemas.broker_activity import (
    BrokerActivityRow,
    ReconciliationTimingPolicy,
)
from app.services.broker_activity_reconciler import (
    EngineIntent,
    ReconciliationContext,
    author_pending_row,
    author_row_from_event,
)
from app.services.broker_activity_templates import (
    _REGISTRY,
    current_version,
    render_template,
)

NS = "learn-ai/sid-property/v1"
INTENT_ID = "intent-prop"
ORDER_REF = f"{NS}:{INTENT_ID}"
TS_MS = 1_700_000_000_000
DEFAULT_POLICY = ReconciliationTimingPolicy()


def _facts_from_row(
    row: BrokerActivityRow, timing_policy: ReconciliationTimingPolicy
) -> dict[str, Any]:
    """Reconstruct the facts dict from the row + the publisher's policy.

    Mirrors ``_facts_for_template`` in reverse: every key any v1 template
    may consume is derivable from ``BrokerActivityRow`` fields plus the
    publisher's stored ``ReconciliationTimingPolicy``. If a future
    template references a key not derivable from this set, the
    truthfulness contract is broken and the property test below fails.

    ``caveat_lag_ms`` is the only non-row input: it lives on the
    publisher's per-instance timing policy, not the row. The policy is
    stable across the lifetime of a run, so audit-replay against the
    same run reproduces identical strings.
    """
    facts: dict[str, Any] = {
        "side": row.side,
        "symbol": row.symbol,
        "quantity": row.quantity,
        "order_type": row.order_type,
    }
    if row.exec_id is not None:
        facts["exec_id"] = row.exec_id
    if row.price is not None:
        facts["price"] = row.price
    if row.commission is not None:
        facts["commission"] = row.commission
    overlay = row.engine_overlay
    if overlay is not None:
        if overlay.requested_qty is not None:
            facts["requested_qty"] = overlay.requested_qty
        if overlay.requested_price is not None:
            facts["requested_price"] = overlay.requested_price
        lag = overlay.lag_breakdown.intent_to_exec_ms
        if lag is not None:
            facts["lag_ms"] = lag
            facts["caveat_lag_ms"] = timing_policy.caveat_lag_ms
    divergence = row.divergence_facts
    if divergence is not None and divergence.price_delta is not None:
        facts["price_delta"] = divergence.price_delta
    return facts


# ── Scenario constructors — one per registered template ────────────────


def _ctx(
    *,
    seq: int = 1,
    seen: frozenset[str] = frozenset(),
    reconnect: bool = False,
    timing_policy: ReconciliationTimingPolicy | None = None,
) -> ReconciliationContext:
    return ReconciliationContext(
        seq=seq,
        ts_ms=TS_MS,
        bot_order_namespace=NS,
        timing_policy=timing_policy or DEFAULT_POLICY,
        previously_seen_exec_ids=seen,
        reconnect_recovery_active=reconnect,
    )


def _intent(**over: Any) -> EngineIntent:
    base: dict[str, Any] = {
        "intent_id": INTENT_ID,
        "requested_qty": 100.0,
        "intent_created_ms": TS_MS - 500,
        "dispatched_ms": TS_MS - 400,
        "acked_ms": TS_MS - 300,
    }
    base.update(over)
    return EngineIntent(**base)


def _fill_event(**over: Any) -> IbkrOrderEvent:
    base: dict[str, Any] = {
        "account_id": "DU1234567",
        "order_id": 42,
        "perm_id": 999,
        "event_type": "fill",
        "status": "Filled",
        "order_ref": ORDER_REF,
        "symbol": "SPY",
        "side": "BUY",
        "order_type": "MKT",
        "exec_id": "exec-prop",
        "fill_quantity": 100.0,
        "avg_fill_price": 450.0,
        "cumulative_filled": 100.0,
        "remaining": 0.0,
        "last_fill_price": 450.0,
        "exec_time_ms": TS_MS - 50,
        "fee": 1.0,
        "ts_ms": TS_MS,
    }
    base.update(over)
    return IbkrOrderEvent(**base)


def _author_normal_fill() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    return (
        author_row_from_event(event=_fill_event(), intent=_intent(), ctx=_ctx()),
        DEFAULT_POLICY,
    )


def _author_pending() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    return (
        author_pending_row(
            intent=_intent(requested_price=450.0),
            symbol="SPY",
            side="BUY",
            quantity=100.0,
            order_type="LMT",
            ctx=_ctx(),
        ),
        DEFAULT_POLICY,
    )


def _author_partial_fill() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    # filled less than requested, with remaining > 0 → partial_fill template.
    return (
        author_row_from_event(
            event=_fill_event(fill_quantity=40.0, cumulative_filled=40.0, remaining=60.0),
            intent=_intent(),
            ctx=_ctx(),
        ),
        DEFAULT_POLICY,
    )


def _author_timing_caveat() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    # intent_to_exec_ms = event.exec_time_ms - intent.intent_created_ms.
    # Target 3_000 ms: between caveat_lag_ms (2_000) and excessive_lag_ms (10_000).
    intent_to_exec_ms = 3_000
    exec_time_ms = TS_MS - 100  # tiny exec→observed slack
    return (
        author_row_from_event(
            event=_fill_event(exec_time_ms=exec_time_ms),
            intent=_intent(intent_created_ms=exec_time_ms - intent_to_exec_ms),
            ctx=_ctx(),
        ),
        DEFAULT_POLICY,
    )


def _author_reconnect_recovery() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    return (
        author_row_from_event(
            event=_fill_event(),
            intent=_intent(),
            ctx=_ctx(reconnect=True),
        ),
        DEFAULT_POLICY,
    )


def _author_missing_commission() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    return (
        author_row_from_event(
            event=_fill_event(fee=None),
            intent=_intent(),
            ctx=_ctx(),
        ),
        DEFAULT_POLICY,
    )


def _author_price_divergence() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    return (
        author_row_from_event(
            event=_fill_event(last_fill_price=451.25, order_type="LMT"),
            intent=_intent(requested_price=450.0),
            ctx=_ctx(),
        ),
        DEFAULT_POLICY,
    )


def _author_quantity_divergence() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    # filled more than requested, terminal (remaining=0) → quantity_divergence.
    return (
        author_row_from_event(
            event=_fill_event(fill_quantity=120.0, cumulative_filled=120.0, remaining=0.0),
            intent=_intent(),
            ctx=_ctx(),
        ),
        DEFAULT_POLICY,
    )


def _author_unmatched_execution() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    # No intent (foreign) → unmatched_execution.
    return (
        author_row_from_event(
            event=_fill_event(order_ref=None),
            intent=None,
            ctx=_ctx(),
        ),
        DEFAULT_POLICY,
    )


def _author_duplicate_execution() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    # exec_id already in previously_seen_exec_ids → duplicate_execution.
    event = _fill_event()
    return (
        author_row_from_event(
            event=event, intent=_intent(), ctx=_ctx(seen=frozenset({event.exec_id or ""}))
        ),
        DEFAULT_POLICY,
    )


def _author_cancellation() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    return (
        author_row_from_event(
            event=_fill_event(
                event_type="cancel",
                status="Cancelled",
                fill_quantity=0.0,
                cumulative_filled=0.0,
                remaining=100.0,
                last_fill_price=None,
                fee=None,
                exec_id=None,
                exec_time_ms=None,
            ),
            intent=_intent(),
            ctx=_ctx(),
        ),
        DEFAULT_POLICY,
    )


def _author_rejection() -> tuple[BrokerActivityRow, ReconciliationTimingPolicy]:
    return (
        author_row_from_event(
            event=_fill_event(
                event_type="error",
                status="Inactive",
                fill_quantity=0.0,
                cumulative_filled=0.0,
                remaining=100.0,
                last_fill_price=None,
                fee=None,
                exec_id=None,
                exec_time_ms=None,
            ),
            intent=_intent(),
            ctx=_ctx(),
        ),
        DEFAULT_POLICY,
    )


# Maps (template_key, template_version) → constructor that authors a row
# rendering that template. Every entry in ``_REGISTRY`` must appear here;
# the registry-completeness test below fails closed if a template is
# added without a corresponding scenario.
_CORPUS: dict[
    tuple[str, int],
    Callable[[], tuple[BrokerActivityRow, ReconciliationTimingPolicy]],
] = {
    ("normal_fill", 1): _author_normal_fill,
    ("pending_acknowledgement", 1): _author_pending,
    ("partial_fill", 1): _author_partial_fill,
    ("timing_caveat", 1): _author_timing_caveat,
    ("reconnect_recovery", 1): _author_reconnect_recovery,
    ("missing_commission", 1): _author_missing_commission,
    ("price_divergence", 1): _author_price_divergence,
    ("quantity_divergence", 1): _author_quantity_divergence,
    ("unmatched_execution", 1): _author_unmatched_execution,
    ("duplicate_execution", 1): _author_duplicate_execution,
    ("cancellation", 1): _author_cancellation,
    ("rejection", 1): _author_rejection,
}


# ── The property test ─────────────────────────────────────────────────


def test_truthfulness_property_corpus_covers_every_current_template() -> None:
    """Every template_key's ``current_version`` must be reachable from the
    corpus. Historical versions in the registry are explicitly allowed to
    be uncovered — ``select_template`` always picks the current version,
    so an authored row can only target ``current_version(key)``. Older
    versions stay registered for audit replay (see ``broker_activity_
    templates`` lines 11-16) and are covered by replay fixtures, not by
    this constructive corpus.

    Fails closed when a template_key's current version ships without a
    scenario, so the property assertion below cannot silently skip
    newly-added templates.
    """
    current_pairs = {(key, current_version(key)) for key, _ in _REGISTRY}
    missing = current_pairs - set(_CORPUS.keys())
    extra = set(_CORPUS.keys()) - set(_REGISTRY.keys())
    assert not missing, (
        f"current-version templates not exercised by the truthfulness corpus: "
        f"{sorted(missing)} — add a scenario constructor to _CORPUS"
    )
    assert not extra, (
        f"corpus references (key, version) pairs not in registry: {sorted(extra)} — "
        f"the templates registry is the source of truth"
    )


@pytest.mark.parametrize(
    "template_key,template_version",
    sorted(_CORPUS.keys()),
)
def test_render_from_row_reproduces_authored_strings(
    template_key: str, template_version: int
) -> None:
    """The truthfulness contract: row + known timing policy → identical
    ``(headline, narrative)`` as the publisher authored.

    Any drift fails this test:
    - A template that references a fact key not derivable from
      ``BrokerActivityRow`` (a hidden engine-state dependency)
    - A renderer that produces non-deterministic strings (e.g. consults
      wall-clock or random state)
    - A change to ``_facts_for_template`` that the row schema doesn't
      reflect (the row stops being self-describing)
    """
    constructor = _CORPUS[(template_key, template_version)]
    row, timing_policy = constructor()

    assert row.template_key == template_key, (
        f"corpus constructor for {template_key} v{template_version} produced a "
        f"row with template_key={row.template_key!r}; the scenario does not "
        f"select the expected template"
    )
    assert row.template_version == template_version

    recovered_facts = _facts_from_row(row, timing_policy)
    headline, narrative = render_template(
        row.template_key, row.template_version, recovered_facts
    )

    assert (headline, narrative) == (row.headline, row.narrative), (
        f"truthfulness contract broken for template {template_key} "
        f"v{template_version}: re-rendering from the row's structured fields "
        f"did not reproduce the authored strings. The row alone is no longer "
        f"sufficient to verify what the operator saw.\n"
        f"  authored headline:  {row.headline!r}\n"
        f"  re-rendered:        {headline!r}\n"
        f"  authored narrative: {row.narrative!r}\n"
        f"  re-rendered:        {narrative!r}\n"
        f"  recovered facts:    {recovered_facts!r}"
    )


def test_facts_from_row_uses_only_row_and_policy_inputs() -> None:
    """Type-level guard: ``_facts_from_row`` accepts only the row and
    the timing policy.

    The truthfulness contract requires that audit replay needs nothing
    beyond what's persisted (the row) and what's part of the publisher's
    own per-instance config (the timing policy). If this signature ever
    grows a new parameter, the row's self-describing property has
    weakened — surface it visibly via this test.
    """
    import inspect

    sig = inspect.signature(_facts_from_row)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["row", "timing_policy"], (
        "the truthfulness contract requires _facts_from_row to depend ONLY "
        "on (row, timing_policy). A new parameter means audit replay now "
        "needs additional state not implied by the row alone — re-evaluate "
        "whether that state belongs on the row instead."
    )


# A static check that every fact key any template requires can be
# produced by ``_facts_from_row`` for at least one corpus row. This
# rules out the case where a fact key is referenced by a template but
# no real row would carry the data to populate it.
def test_every_template_required_fact_key_is_produced_by_some_corpus_row() -> None:
    referenced_keys: set[str] = set()
    for template in _REGISTRY.values():
        referenced_keys |= set(template.required_fact_keys)

    produced_keys: set[str] = set()
    for constructor in _CORPUS.values():
        row, timing_policy = constructor()
        produced_keys |= set(_facts_from_row(row, timing_policy).keys())

    missing = referenced_keys - produced_keys
    assert not missing, (
        f"template required_fact_keys not derivable from any corpus row: "
        f"{sorted(missing)}. Either the row schema is missing a field the "
        f"template needs, or no scenario exercises a row that carries it."
    )


# Re-exporting facts-from-row for any future test that wants to use it
# without depending on the private name (the inverse function IS the
# contract — make it discoverable).
def facts_from_row(
    row: BrokerActivityRow, timing_policy: ReconciliationTimingPolicy
) -> Mapping[str, Any]:
    """Public alias of the row→facts inverse used in this test.

    Other test modules that want to assert the same truthfulness
    property on their own fixtures should import this rather than
    reach into ``_facts_from_row``.
    """
    return _facts_from_row(row, timing_policy)
