"""Versioned operator-facing templates for broker-activity rows (ADR 0014 §3).

Every ``(template_key, template_version)`` pair maps to a deterministic
pure ``render(facts) -> (headline, narrative)`` function. The reconciler
calls ``render_template`` with the row's structured facts; the result is
frozen into the persisted row.

Discipline:

- Templates may reference ONLY structured facts present on the row.
- A v2 of a template ships as a new ``Template`` constant; v1 stays
  registered so historical rows (which carry their template_version)
  remain renderable for audit replay.
- ``current_version(key)`` returns the version that newly-authored rows
  should use. Historical rows resolve via their stored
  ``(template_key, template_version)`` pair.
- Adding a new ``ReasonCode`` requires a corresponding template entry
  reachable via ``select_template``; the test suite enforces this so
  the truthfulness contract cannot drift.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


class TemplateNotFoundError(KeyError):
    """Raised when ``(template_key, template_version)`` is not registered.

    Never silently fall back to a generic template — that would let the
    operator see an authored string disconnected from the registered
    rendering. Raise loudly so the bug surfaces at write or read time.
    """


class MissingFactError(KeyError):
    """Raised when a template's required fact keys are not all present.

    Templates whitelist the fact keys they consume; rendering with a
    subset is a programming error, not an operator-visible event. The
    publisher converts this into a halt-and-log path rather than
    authoring a partially-rendered string.
    """


def _fmt_qty(qty: float) -> str:
    """Render a share quantity. Integer-valued floats render without
    decimals; fractional shares keep up to four decimals (paper-trading
    allows fractional sizing for some symbols)."""
    if qty == int(qty):
        return f"{int(qty)}"
    return f"{qty:.4f}".rstrip("0").rstrip(".")


def _fmt_price(price: float) -> str:
    return f"${price:.2f}"


def _fmt_lag(lag_ms: int) -> str:
    """Operator-friendly lag rendering. < 1s → ms, otherwise seconds."""
    if lag_ms < 1_000:
        return f"{lag_ms}ms"
    return f"{lag_ms / 1000:.1f}s"


Renderer = Callable[[Mapping[str, Any]], tuple[str, str]]


@dataclass(frozen=True)
class Template:
    key: str
    version: int
    required_fact_keys: frozenset[str]
    render: Renderer


# ── v1 template renderers ───────────────────────────────────────────────


def _r_normal_fill(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = (
        f"Filled {_fmt_qty(f['quantity'])} {f['symbol']} at {_fmt_price(f['price'])}"
    )
    commission_str = (
        f"; {_fmt_price(f['commission'])} commission"
        if f.get("commission") is not None
        else ""
    )
    narrative = (
        f"{f['order_type']} order filled in full at {_fmt_price(f['price'])}"
        f"{commission_str}."
    )
    return headline, narrative


def _r_pending_acknowledgement(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = f"Pending {f['side'].lower()} of {_fmt_qty(f['quantity'])} {f['symbol']}"
    narrative = (
        f"Intent submitted to the broker; awaiting acknowledgement. "
        f"{f['order_type']} order."
    )
    return headline, narrative


def _r_partial_fill(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = (
        f"Partial fill: {_fmt_qty(f['quantity'])} of "
        f"{_fmt_qty(f['requested_qty'])} {f['symbol']} at {_fmt_price(f['price'])}"
    )
    narrative = (
        f"{f['order_type']} order filled "
        f"{_fmt_qty(f['quantity'])} of {_fmt_qty(f['requested_qty'])} shares "
        f"at {_fmt_price(f['price'])}; remaining quantity not filled."
    )
    return headline, narrative


def _r_timing_caveat(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = (
        f"Filled {_fmt_qty(f['quantity'])} {f['symbol']} at {_fmt_price(f['price'])} "
        f"({_fmt_lag(f['lag_ms'])} after intent)"
    )
    narrative = (
        f"{f['order_type']} order filled at {_fmt_price(f['price'])}. "
        f"Execution arrived {_fmt_lag(f['lag_ms'])} after the engine emitted the "
        f"intent — above the configured caveat threshold "
        f"({_fmt_lag(f['caveat_lag_ms'])}); no other divergence."
    )
    return headline, narrative


def _r_reconnect_recovery(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = (
        f"Recovered fill of {_fmt_qty(f['quantity'])} {f['symbol']} at "
        f"{_fmt_price(f['price'])} (captured on reconnect)"
    )
    narrative = (
        f"{f['order_type']} order filled at {_fmt_price(f['price'])} during the "
        f"broker reconnect window. Execution was captured via reqExecutions on "
        f"resume; the operator-facing lag reflects observation, not exchange time."
    )
    return headline, narrative


def _r_missing_commission(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = (
        f"Filled {_fmt_qty(f['quantity'])} {f['symbol']} at {_fmt_price(f['price'])} "
        f"(commission pending)"
    )
    narrative = (
        f"{f['order_type']} order filled in full at {_fmt_price(f['price'])}. "
        f"IBKR has not yet reported the commission for this execution; the row "
        f"will not be re-authored when the commission arrives — see the durable "
        f"audit log for the final fee."
    )
    return headline, narrative


def _r_price_divergence(f: Mapping[str, Any]) -> tuple[str, str]:
    direction = "above" if f["price_delta"] > 0 else "below"
    headline = (
        f"Price divergence: filled {_fmt_qty(f['quantity'])} {f['symbol']} at "
        f"{_fmt_price(f['price'])} ({_fmt_price(abs(f['price_delta']))} {direction} "
        f"requested {_fmt_price(f['requested_price'])})"
    )
    narrative = (
        f"{f['order_type']} order filled at {_fmt_price(f['price'])}, "
        f"{_fmt_price(abs(f['price_delta']))} {direction} the requested "
        f"{_fmt_price(f['requested_price'])}."
    )
    return headline, narrative


def _r_quantity_divergence(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = (
        f"Quantity divergence: filled {_fmt_qty(f['quantity'])} of "
        f"{_fmt_qty(f['requested_qty'])} {f['symbol']}"
    )
    narrative = (
        f"{f['order_type']} order filled {_fmt_qty(f['quantity'])} shares; engine "
        f"intent requested {_fmt_qty(f['requested_qty'])}. Investigate broker-side "
        f"sizing rules (cash buffer, position cap) or engine-side rounding."
    )
    return headline, narrative


def _r_unmatched_execution(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = (
        f"Unmatched execution: {f['side']} {_fmt_qty(f['quantity'])} {f['symbol']} "
        f"at {_fmt_price(f['price'])}"
    )
    narrative = (
        f"IBKR reported a {f['side'].lower()} of {_fmt_qty(f['quantity'])} "
        f"{f['symbol']} at {_fmt_price(f['price'])} that does not match any "
        f"engine-emitted intent in this instance's namespace. Possible causes: "
        f"manual TWS click on the same account, stale order from a prior run, "
        f"or a foreign client_id under the DU account."
    )
    return headline, narrative


def _r_duplicate_execution(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = (
        f"Duplicate execution suppressed: {f['symbol']} exec {f['exec_id']}"
    )
    narrative = (
        f"IBKR redelivered an execution ({f['exec_id']}) the publisher has "
        f"already authored. The duplicate is logged for audit and not "
        f"re-emitted to the operator stream."
    )
    return headline, narrative


def _r_cancellation(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = f"Cancelled {f['side'].lower()} of {_fmt_qty(f['quantity'])} {f['symbol']}"
    narrative = (
        f"{f['order_type']} order cancelled before fill. "
        f"No position change."
    )
    return headline, narrative


def _r_rejection(f: Mapping[str, Any]) -> tuple[str, str]:
    headline = f"Rejected {f['side'].lower()} of {_fmt_qty(f['quantity'])} {f['symbol']}"
    narrative = (
        f"Broker rejected the {f['order_type']} order. "
        f"No position change. See the durable mutation log for the broker's "
        f"reason code."
    )
    return headline, narrative


# ── Registry — every (key, version) pair lives here ──────────────────────

_TEMPLATES_V1: tuple[Template, ...] = (
    Template(
        key="normal_fill",
        version=1,
        required_fact_keys=frozenset({"quantity", "symbol", "price", "order_type"}),
        render=_r_normal_fill,
    ),
    Template(
        key="pending_acknowledgement",
        version=1,
        required_fact_keys=frozenset({"side", "quantity", "symbol", "order_type"}),
        render=_r_pending_acknowledgement,
    ),
    Template(
        key="partial_fill",
        version=1,
        required_fact_keys=frozenset(
            {"quantity", "requested_qty", "symbol", "price", "order_type"}
        ),
        render=_r_partial_fill,
    ),
    Template(
        key="timing_caveat",
        version=1,
        required_fact_keys=frozenset(
            {"quantity", "symbol", "price", "lag_ms", "caveat_lag_ms", "order_type"}
        ),
        render=_r_timing_caveat,
    ),
    Template(
        key="reconnect_recovery",
        version=1,
        required_fact_keys=frozenset(
            {"quantity", "symbol", "price", "order_type"}
        ),
        render=_r_reconnect_recovery,
    ),
    Template(
        key="missing_commission",
        version=1,
        required_fact_keys=frozenset({"quantity", "symbol", "price", "order_type"}),
        render=_r_missing_commission,
    ),
    Template(
        key="price_divergence",
        version=1,
        required_fact_keys=frozenset(
            {"quantity", "symbol", "price", "price_delta", "requested_price", "order_type"}
        ),
        render=_r_price_divergence,
    ),
    Template(
        key="quantity_divergence",
        version=1,
        required_fact_keys=frozenset(
            {"quantity", "requested_qty", "symbol", "order_type"}
        ),
        render=_r_quantity_divergence,
    ),
    Template(
        key="unmatched_execution",
        version=1,
        required_fact_keys=frozenset({"side", "quantity", "symbol", "price"}),
        render=_r_unmatched_execution,
    ),
    Template(
        key="duplicate_execution",
        version=1,
        required_fact_keys=frozenset({"symbol", "exec_id"}),
        render=_r_duplicate_execution,
    ),
    Template(
        key="cancellation",
        version=1,
        required_fact_keys=frozenset({"side", "quantity", "symbol", "order_type"}),
        render=_r_cancellation,
    ),
    Template(
        key="rejection",
        version=1,
        required_fact_keys=frozenset({"side", "quantity", "symbol", "order_type"}),
        render=_r_rejection,
    ),
)


_REGISTRY: dict[tuple[str, int], Template] = {
    (t.key, t.version): t for t in _TEMPLATES_V1
}


def current_version(template_key: str) -> int:
    """Return the version newly-authored rows should use for ``template_key``.

    For v1-only templates this is 1. When a v2 ships, this function
    returns 2; historical v1 rows still render correctly because they
    persist their own ``template_version``.
    """
    versions = [v for (k, v) in _REGISTRY if k == template_key]
    if not versions:
        raise TemplateNotFoundError(template_key)
    return max(versions)


def render_template(
    template_key: str,
    template_version: int,
    facts: Mapping[str, Any],
) -> tuple[str, str]:
    """Render ``(headline, narrative)`` from the row's structured facts.

    Raises ``TemplateNotFoundError`` if the pair is not registered, or
    ``MissingFactError`` if the template's required keys are not all
    present. Never returns a partial render or silently substitutes
    placeholder text.
    """
    template = _REGISTRY.get((template_key, template_version))
    if template is None:
        raise TemplateNotFoundError((template_key, template_version))
    missing = template.required_fact_keys - facts.keys()
    if missing:
        raise MissingFactError(
            f"template {template_key} v{template_version} requires "
            f"facts {sorted(template.required_fact_keys)}; missing "
            f"{sorted(missing)}"
        )
    return template.render(facts)


def registered_keys() -> frozenset[str]:
    """Set of every registered template key (across all versions).

    Used by the reconciler-test suite to assert the template library is
    complete for every ``ReasonCode`` that ``select_template`` can
    return.
    """
    return frozenset(k for (k, _v) in _REGISTRY)
