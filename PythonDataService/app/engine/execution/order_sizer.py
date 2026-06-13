"""Live policy-application adapter for ``set_holdings`` sizing — ADR 0009.

The **canonical live policy-application layer**. ``LiveConfig.sizing`` carries
the operator-selected policy; the engine threads it into the ``LivePortfolio``,
and this module reinterprets every ``set_holdings(symbol, fraction)`` call per
the policy's ``kind``. ``set_holdings`` is the **only** order surface this
policy governs — ``market_order``, ``liquidate`` and options ``contracts_per_trade``
are explicit strategy sizing and pass through untouched.

This is an **adapter**, not a parallel hierarchy: the percent path (``SetHoldings``)
delegates to ``app.engine.execution.sizing.LeanSetHoldingsSizing`` — the existing
canonical ``SetHoldings`` quantity-math authority. The wiring happens in PR2;
PR1 ships ``FixedShares`` only.

Four kinds, validated by Pydantic discriminated union at both untyped boundaries
the code has today (``HostRunnerDeployRequest.live_config`` at the deploy API,
and ``_live_config_from_ledger`` at run start):

* ``FixedShares(value: int >= 1)`` — target ``value`` shares (long-only in v1).
* ``SetHoldings(fraction: Decimal in (0, 1])`` — percent of portfolio value.
  Routes through ``LeanSetHoldingsSizing`` (wired in PR2).
* ``FixedNotional(value: Decimal as string)`` — ``floor(value / price)`` shares.
  Wired in PR4.
* ``StrategyExplicit`` — strategy supplies its own quantity/contracts; the
  policy imposes no sizing (the **honest** ledger value for explicit-surface
  registrations, never a misleading ``FixedShares(1)``).

PR1 only ships ``FixedShares`` runtime resolution. The other three kinds parse
and validate cleanly; calling ``resolve_set_holdings_quantity`` on them raises
``SizingKindNotWiredError`` with the PR that will wire them.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.sizing import LeanSetHoldingsSizing


class SizingKindNotWiredError(NotImplementedError):
    """A ``SizingPolicy`` kind validates but its runtime resolver has not shipped.

    Raised by ``OrderSizer.resolve_set_holdings_quantity`` for ``SetHoldings``
    / ``FixedNotional`` / ``StrategyExplicit`` in PR1. Carries the kind and the
    PR identifier that will wire it so the error message is actionable.
    """

    def __init__(self, kind: str, lands_in_pr: str) -> None:
        super().__init__(
            f"sizing kind {kind!r} validates but has no runtime resolver in PR1; "
            f"it lands in {lands_in_pr}"
        )
        self.kind = kind
        self.lands_in_pr = lands_in_pr


class _SizingBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FixedShares(_SizingBase):
    """Target a fixed integer share count. ``fraction > 0`` ⇒ ``value`` shares;
    ``fraction == 0`` ⇒ flat. Long-only in v1."""

    kind: Literal["FixedShares"] = "FixedShares"
    value: int = Field(ge=1)


class SetHoldings(_SizingBase):
    """Target a fraction of portfolio value. Routes through
    ``LeanSetHoldingsSizing`` (wired in PR2)."""

    kind: Literal["SetHoldings"] = "SetHoldings"
    fraction: Decimal = Field(gt=Decimal(0), le=Decimal(1))

    @field_validator("fraction", mode="before")
    @classmethod
    def _coerce_fraction(cls, value: object) -> Decimal:
        # Forbid float on the wire — money/percent values must arrive as a
        # decimal string (or already a Decimal) so wire-format float-noise
        # never leaks into the hashed ``live_config``.
        if isinstance(value, Decimal):
            return value
        if isinstance(value, str):
            try:
                return Decimal(value)
            except InvalidOperation as exc:
                raise ValueError(f"SetHoldings.fraction not a valid decimal: {value!r}") from exc
        if isinstance(value, int):
            return Decimal(value)
        raise TypeError(
            f"SetHoldings.fraction must be a decimal string or int, got {type(value).__name__}"
        )


class FixedNotional(_SizingBase):
    """Target an absolute dollar notional. ``floor(value / price)`` shares.
    Wired in PR4."""

    kind: Literal["FixedNotional"] = "FixedNotional"
    value: Decimal = Field(gt=Decimal(0))

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, value: object) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, str):
            try:
                return Decimal(value)
            except InvalidOperation as exc:
                raise ValueError(f"FixedNotional.value not a valid decimal: {value!r}") from exc
        raise TypeError(
            "FixedNotional.value must be a decimal string (float is banned on the wire "
            "to preserve hashing stability of live_config)"
        )


class StrategyExplicit(_SizingBase):
    """The strategy supplies its own quantity/contracts; the policy imposes
    no sizing. The honest ledger value for ``sizing_surface=explicit``
    registrations."""

    kind: Literal["StrategyExplicit"] = "StrategyExplicit"


SizingPolicy = Annotated[
    FixedShares | SetHoldings | FixedNotional | StrategyExplicit,
    Field(discriminator="kind"),
]


class _SizingPolicyWrapper(BaseModel):
    """Internal Pydantic adapter — lets callers parse a sizing dict through
    the discriminated union without instantiating Annotated directly."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    policy: SizingPolicy


def parse_sizing_policy(payload: object) -> SizingPolicy:
    """Validate a raw dict (e.g. from ledger JSON) into a ``SizingPolicy``.

    Raises ``ValueError`` on invalid input — surfaces a Pydantic validation
    error wrapped as the standard exception the deploy and start gates already
    catch. The empty-dict / missing-``kind`` case is rejected here; absence of
    ``sizing`` is handled by the caller (it means ``legacy/unknown`` per ADR 0009).
    """
    try:
        return _SizingPolicyWrapper.model_validate({"policy": payload}).policy
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid live_config.sizing: {exc}") from exc


def policy_to_ledger_dict(policy: SizingPolicy) -> dict:
    """Serialize a policy to the canonical dict form persisted in the ledger.

    Uses ``mode='json'`` so ``Decimal`` becomes a string — the wire/storage
    rule for money/percent values, preserving hash stability of the
    content-addressed ``run_id``.
    """
    return policy.model_dump(mode="json")


def governed_by(policy: SizingPolicy | None) -> Literal["live_config", "strategy_explicit"]:
    """Engine-derived ``governed_by`` ledger stamp (ADR 0009 § 3).

    ``StrategyExplicit`` ⇒ ``strategy_explicit`` (the strategy sized itself);
    every other kind ⇒ ``live_config`` (the deploy-page policy sized via
    ``set_holdings``). ``None`` (legacy/unknown ledger) ⇒ ``live_config`` —
    the absence-of-policy era was de facto governed by the legacy
    ``SimpleFloorSizing`` ``set_holdings`` path.
    """
    if isinstance(policy, StrategyExplicit):
        return "strategy_explicit"
    return "live_config"


def default_sizing_provenance(policy: SizingPolicy | None) -> Literal["live_override"]:
    """Engine-derived ``sizing_provenance`` for PR1 — always ``live_override``.

    PR3 introduces the audit-copy allow-list and the three-state proof
    (proven match / proven mismatch / cannot prove); until then there is no
    proof path, so the fail-closed default applies to every policy. ``spec_default``
    is reserved (ADR 0009 § 3) and not emitted today.
    """
    return "live_override"


class PortfolioValueProvider(Protocol):
    """Pass-through seam for the future capital-sleeve layer (ADR 0009 § 9).

    PR1 ships a pass-through implementation that returns the whole-account
    portfolio value; the per-strategy sleeve is a later drop-in at this seam.
    ``FixedShares`` / ``FixedNotional`` never read the provider — only
    ``SetHoldings`` (the percent path, wired in PR2) does.
    """

    def portfolio_value(self) -> Decimal:
        """Total portfolio value the percent-path resolver may target."""
        ...


class WholeAccountPortfolioValueProvider:
    """PR1 default: the whole-account portfolio value, no sleeve.

    Takes a ``callable`` that resolves the current portfolio value on every
    invocation — ``LivePortfolio.total_value`` walks cash + positions at
    the latest reference price, so the percent path always reads fresh data.
    The capital-sleeve layer will drop in at this seam (ADR 0009 § 9)
    without a runtime API change for ``OrderSizer``.
    """

    name: str = "whole_account"

    def __init__(self, get_total: Callable[[], Decimal]) -> None:
        self._get_total = get_total

    def portfolio_value(self) -> Decimal:
        return self._get_total()


class OrderSizer:
    """Live policy-application adapter for ``set_holdings`` (ADR 0009 § 5).

    Holds the resolved ``SizingPolicy`` and the (future-sleeve-ready) portfolio
    value provider. The percent path delegates to ``LeanSetHoldingsSizing``
    (PR2 wire-up) — the canonical golden-fixture-pinned quantity-math authority.
    """

    def __init__(
        self,
        policy: SizingPolicy,
        portfolio_value_provider: PortfolioValueProvider | None = None,
    ) -> None:
        self._policy = policy
        self._portfolio_value_provider = portfolio_value_provider
        # PR2 — the canonical percent-path resolver. Single instance per
        # ``OrderSizer`` so the IBKR commission model isn't re-constructed
        # on every bar.
        self._lean_sizing = LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())

    @property
    def policy(self) -> SizingPolicy:
        return self._policy

    def resolve_set_holdings_quantity(
        self,
        *,
        target_fraction: Decimal,
        reference_price: Decimal | None = None,
        order_fee: Decimal = Decimal(0),
    ) -> int:
        """Translate ``set_holdings(symbol, fraction)`` into a target share count.

        ``target_fraction`` carries the strategy's *direction intent* (``> 0``
        ⇒ long target, ``== 0`` ⇒ flat). The policy reinterprets the magnitude
        per kind.

        ``reference_price`` is required for the ``SetHoldings`` percent path
        (the Lean resolver needs the price + portfolio_value to compute the
        buffered share count) and for ``FixedNotional`` (which floors
        ``value / price``). ``FixedShares`` ignores it.

        Returns ``0`` for a flat target. The caller (``LivePortfolio.set_holdings``)
        is responsible for converting target → delta and skipping submission
        when the delta would be a zero-quantity order — the engine logs a
        *sizing skip* there, not here.
        """
        policy = self._policy
        if isinstance(policy, FixedShares):
            if target_fraction < Decimal(0):
                # Long-only in v1 — a negative fraction is a short intent that
                # FixedShares would otherwise silently invert to a positive
                # target quantity. Fail fast so a misconfigured strategy never
                # opens a short position.
                raise ValueError(
                    "FixedShares is long-only in v1; target_fraction must be >= 0, "
                    f"got {target_fraction}"
                )
            if target_fraction == Decimal(0):
                return 0
            return int(policy.value)
        if isinstance(policy, StrategyExplicit):
            # Not reachable from set_holdings in a well-registered system:
            # an `explicit`-surface strategy never calls set_holdings (Decision 6
            # makes it a fail-fast registration bug if it does). Until PR7 wires
            # the order-surface fail-fast, this raises rather than silently
            # passing through.
            raise SizingKindNotWiredError("StrategyExplicit", "PR7")
        if isinstance(policy, SetHoldings):
            if target_fraction == Decimal(0):
                return 0
            if reference_price is None:
                raise ValueError(
                    "SetHoldings sizing requires a reference price; "
                    "LivePortfolio must update_reference_price(...) before set_holdings."
                )
            if self._portfolio_value_provider is None:
                raise RuntimeError(
                    "SetHoldings sizing requires a PortfolioValueProvider; "
                    "construct OrderSizer with WholeAccountPortfolioValueProvider(...)."
                )
            # Magnitude is the policy fraction, NOT the caller's direction
            # signal — the strategy passes ``1`` to mean "go long, target the
            # SetHoldings policy"; the policy maps that to its own fraction.
            policy_fraction = policy.fraction
            return self._lean_sizing.target_quantity(
                portfolio_value=self._portfolio_value_provider.portfolio_value(),
                price=reference_price,
                target_fraction=policy_fraction,
                order_fee=order_fee,
            )
        if isinstance(policy, FixedNotional):
            raise SizingKindNotWiredError("FixedNotional", "PR4")
        raise SizingKindNotWiredError(type(policy).__name__, "unknown")
