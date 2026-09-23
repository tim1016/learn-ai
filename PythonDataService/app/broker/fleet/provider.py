"""The provider-adapter protocol, the typed operation catalog and the
code-owned production registry.

PRD §9.1: the fleet package defines a narrow protocol; the first production
adapter will be Alpaca (Phase 2). Test-only fakes are injectable through
constructor injection and never enter the production mapping. A provider
adapter declares its immutable provider ID, adapter version, canonical
account-ID function, typed capabilities, typed operation catalog and
provider-authored summary shape; the provider alone owns its clients,
credentials, verification, commands, custody, receipts, risk, arming,
recovery and tests (FR-004).

The generic layer routes and verifies; it does not trade. It never translates
one provider's operation into another's, and it never infers that a capability
declared by one provider exists for another (FR-006).

The operation catalog (audit 2026-09-13, finding 6) is the *single* contract:
the coordinator's forwarding allowlist, the agent's mounts, the exported
OpenAPI and the generated frontend builders all derive from the provider's
typed declarations, so no second hand-maintained route list can drift from
the handlers that actually serve an operation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.broker.fleet.errors import (
    BrokerNotSupported,
    FleetProtocolIncompatible,
)
from app.broker.fleet.internal_http import DEFAULT_INTERNAL_TIMEOUT_S

#: The fleet protocol this build speaks. An agent registering under a
#: different protocol version refuses explicitly — a newer coordinator must
#: not advertise operations to an older agent merely because its own adapter
#: knows them (audit 2026-09-13, finding 6).
FLEET_PROTOCOL_VERSION = 2


class Capability(StrEnum):
    """The closed typed operation vocabulary a provider adapter may declare.

    Capability is evidence: an action whose capability is not declared by the
    concrete provider is a typed refusal, never an emulation or a downgrade.
    The vocabulary is widened only here, in code review, never per request.
    """

    ACCOUNT_READ = "account_read"
    POSITIONS_READ = "positions_read"
    ORDERS_READ = "orders_read"
    MARKET_STATUS_READ = "market_status_read"
    CONFIGURATION_MANAGE = "configuration_manage"
    BOT_PANEL_READ = "bot_panel_read"
    BOT_ACTION = "bot_action"
    DEPLOY = "deploy"
    CUSTODY_READ = "custody_read"
    CUSTODY_COMMAND = "custody_command"
    GALLERY_READ = "gallery_read"
    MANUAL_ORDERS = "manual_orders"
    STREAM_SUBSCRIBE = "stream_subscribe"


class OperationReadiness(StrEnum):
    """Whether an operation needs a confirmed binding to be routable.

    ``configuration_access`` keeps a provisioned but unbound lane servable —
    the operator must be able to inspect and repair the configuration that
    would produce a binding. ``execution`` requires the confirmed effective
    account and binding generation plus every provider gate.
    """

    CONFIGURATION_ACCESS = "configuration_access"
    EXECUTION = "execution"


class OperationIdempotency(StrEnum):
    """How repeats of one operation are made safe.

    ``read`` repeats freely. ``durable_key`` mutations carry a caller-minted
    idempotency identity reconciled by the provider clerk's own command
    machinery. ``one_shot`` lifecycle transitions are fenced by provider-side
    state instead of a caller key.
    """

    READ = "read"
    DURABLE_KEY = "durable_key"
    ONE_SHOT = "one_shot"


class OperationStream(StrEnum):
    """Whether an operation returns one response or a long-lived event stream."""

    NONE = "none"
    SSE = "sse"


class OperationDrainAdmission(StrEnum):
    """Whether a draining lane still routes this mutation (#2351).

    A draining lane accepts no new work, but ADR 0063 §2's amendment has the
    operator make it quiet with panel actions — stop, cancel the verified
    working orders, flatten and stop — and the lane must stay readable while
    they do. ``quiesce`` marks a mutation that can only stop a bot or reduce
    exposure, never open it; everything else refuses while draining. Reads
    need no declaration: ``read`` idempotency already says they change
    nothing (``ProviderOperation.routable_while_draining``).
    """

    REFUSED = "refused"
    QUIESCE = "quiesce"


_OPERATION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
#: Path parameters may carry Starlette-style converters (``{order_ref:path}``);
#: the converter is routing syntax, not part of the parameter identity.
_PATH_PARAM_PATTERN = re.compile(r"\{[a-z_][a-z0-9_]*(?::[a-z]+)?\}")
#: POST is excluded: a POST may be a query over a body (a plan, a diagnostic
#: check) whose handler computes over durable state without mutating it, and
#: those carry read idempotency honestly. PUT/PATCH/DELETE are state-setting
#: by construction.
_MUTATING_METHODS = frozenset({"PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class ProviderOperation:
    """One provider-declared operation of a clerk's served surface.

    ``path_template`` is the public path relative to the clerk scope
    (``/api/brokers/{broker}/clerks/{clerk_id}`` is prefixed by the
    coordinator); ``agent_path_template`` is the path the provider's agent
    process actually serves. Both are declared here so forwarding, contract
    export and frontend builders derive from one source.
    """

    operation_id: str
    method: str
    path_template: str
    agent_path_template: str
    capability: Capability
    readiness: OperationReadiness
    requires_effective_account: bool
    idempotency: OperationIdempotency
    stream: OperationStream = OperationStream.NONE
    request_schema: str | None = None
    response_schema: str | None = None
    #: This operation's own outer coordinator -> agent delivery read-timeout
    #: bound. Defaults to the fleet default
    #: (``app.broker.fleet.internal_http.DEFAULT_INTERNAL_TIMEOUT_S``) rather
    #: than ``None``: ``DeliveryRequest.operation`` already carries this
    #: value, so ``HttpLaneDelivery.deliver`` reads it directly instead of a
    #: separately-threaded kwarg that could silently drift from it (issue
    #: #2204 gate F3). A generic knob, not a special case: the
    #: routing/delivery layer only honors whatever an operation declares here
    #: (issue #2204's ``bot_chart_history`` is the first and, as of this
    #: writing, only operation that widens it) -- it never special-cases an
    #: operation id.
    read_timeout_s: float = DEFAULT_INTERNAL_TIMEOUT_S
    drain_admission: OperationDrainAdmission = OperationDrainAdmission.REFUSED

    def route_key(self) -> tuple[str, str]:
        """The (method, public path template) pair forwarding matches on."""
        return (self.method, self.path_template)

    @property
    def routable_while_draining(self) -> bool:
        """The one allow-list a draining lane routes by (#2351, ADR 0063 §2).

        A read, or a mutation declared ``quiesce``. Bot starts, arming,
        deploys, manual orders and configuration writes are none of these,
        so a drained lane keeps refusing everything that could open exposure.
        """
        return (
            self.idempotency == OperationIdempotency.READ
            or self.drain_admission == OperationDrainAdmission.QUIESCE
        )


def validate_operation_catalog(operations: frozenset[ProviderOperation]) -> None:
    """Refuse a catalog that could not be one source of routing truth.

    Checks identity uniqueness (the operation id and the parameter-normalized
    public route), template shape, parameter-set agreement between the public
    and agent templates, and that the declared idempotency, stream and
    readiness facts agree with each other and the HTTP method. A provider
    whose catalog fails this never enters a registry — the checks run in
    adapter conformance tests and at adapter registration.
    """
    seen_ids: set[str] = set()
    seen_routes: set[tuple[str, str]] = set()
    for operation in operations:
        if _OPERATION_ID_PATTERN.fullmatch(operation.operation_id) is None:
            raise ValueError(
                f"operation id {operation.operation_id!r} is not a bounded snake_case token"
            )
        if operation.method.upper() != operation.method or operation.method not in (
            "GET", "POST", "PUT", "PATCH", "DELETE"
        ):
            raise ValueError(f"operation {operation.operation_id!r} declares method {operation.method!r}")
        public_parameters: set[str] = set()
        agent_parameters: set[str] = set()
        for label, template, collected in (
            ("path_template", operation.path_template, public_parameters),
            ("agent_path_template", operation.agent_path_template, agent_parameters),
        ):
            if not template.startswith("/") or template.endswith("/") or "//" in template:
                raise ValueError(
                    f"operation {operation.operation_id!r} {label} {template!r} must be an "
                    "absolute path without trailing or duplicate slashes"
                )
            for parameter in _PATH_PARAM_PATTERN.findall(template):
                if template.count(parameter) > 1:
                    raise ValueError(
                        f"operation {operation.operation_id!r} repeats path parameter {parameter}"
                    )
                collected.add(parameter.strip("{}").split(":", 1)[0])
        if public_parameters != agent_parameters:
            raise ValueError(
                f"operation {operation.operation_id!r} declares different path parameters "
                f"for its public ({sorted(public_parameters)}) and agent "
                f"({sorted(agent_parameters)}) templates; forwarding cannot populate both"
            )
        if (
            operation.readiness == OperationReadiness.CONFIGURATION_ACCESS
            and operation.requires_effective_account
        ):
            raise ValueError(
                f"configuration-access operation {operation.operation_id!r} cannot "
                "require an effective account: configuration access exists precisely "
                "for lanes without one"
            )
        if operation.read_timeout_s <= 0:
            raise ValueError(
                f"operation {operation.operation_id!r} declares a non-positive "
                f"read_timeout_s of {operation.read_timeout_s!r}"
            )
        if operation.idempotency == OperationIdempotency.READ and operation.method in _MUTATING_METHODS:
            raise ValueError(
                f"mutating operation {operation.operation_id!r} cannot declare read idempotency"
            )
        if (
            operation.drain_admission == OperationDrainAdmission.QUIESCE
            and operation.idempotency == OperationIdempotency.READ
        ):
            raise ValueError(
                f"read operation {operation.operation_id!r} cannot declare quiesce drain "
                "admission: a read already routes while draining, and quiesce names "
                "only mutations that stop a bot or reduce exposure"
            )
        if operation.stream == OperationStream.SSE and operation.method != "GET":
            raise ValueError(f"streaming operation {operation.operation_id!r} must be a GET")
        if operation.operation_id in seen_ids:
            raise ValueError(f"operation id {operation.operation_id!r} is declared twice")
        # Uniqueness compares the parameter-normalized shape: ``/bots/{sid}``
        # and ``/bots/{bot_id}`` match one request space and must not both exist.
        normalized_route = (
            operation.method,
            _PATH_PARAM_PATTERN.sub("{}", operation.path_template),
        )
        if normalized_route in seen_routes:
            raise ValueError(f"route {operation.route_key()} is declared twice")
        seen_ids.add(operation.operation_id)
        seen_routes.add(normalized_route)


@dataclass(frozen=True, slots=True)
class ServedContext:
    """The identity a routed operation must be checked against (PRD FR-075).

    The agent-side validation hook receives this; the coordinator-side checks
    (broker/clerk/epoch/generation) live in the control service. Kept in this
    package so provider adapters validate against one shared shape instead of
    inventing per-provider context types.
    """

    broker: str
    clerk_id: str
    agent_instance_id: str
    routing_epoch: int
    account_id: str | None
    capability: Capability
    effective_binding_generation: int


@runtime_checkable
class BrokerProviderAdapter(Protocol):
    """What a broker-owned clerk implementation presents to the fleet spine."""

    @property
    def provider_id(self) -> str:
        """The immutable, code-owned provider identity this adapter serves."""
        ...

    @property
    def adapter_version(self) -> str:
        """The provider implementation's own version string."""
        ...

    @property
    def capabilities(self) -> frozenset[Capability]:
        """The closed set of typed operations this provider declares."""
        ...

    def operations(self) -> frozenset[ProviderOperation]:
        """The typed operation catalog this provider's clerk serves.

        The single source the coordinator allowlist, agent mounts, exported
        contracts and generated frontend builders derive from (audit
        2026-09-13, finding 6). Every operation's capability must appear in
        ``capabilities``; an empty catalog means the provider serves no
        operations yet.
        """
        ...

    def canonical_account_id(self, external_account_id: str) -> str:
        """Canonicalize one external account ID under this provider's rules.

        The provider alone defines canonicity (PRD FR-051); the registry treats
        the result as opaque. Two providers may canonicalize the same raw
        string differently, which is why assignment uniqueness is
        provider-qualified.
        """
        ...

    def provider_summary(self, observation: Mapping[str, object]) -> Mapping[str, object]:
        """Author the provider-typed summary for the directory.

        The returned mapping is the provider's own vocabulary (for Alpaca,
        endpoint mode and authority state, per PRD §10.1). The fleet directory
        does not interpret its financial contents; the observation carries only
        facts the registry already holds (reported state, account, binding
        generation, the typed lane summary the agent reported).
        """
        ...

    def validate_served_context(self, context: ServedContext) -> None:
        """Refuse a served context this provider cannot honor (PRD FR-003).

        Provider-owned safety gates (for Alpaca: mode agreement, arming,
        envelope) answer here; the generic layer has already checked broker,
        clerk, epoch and capability. A raised refusal's text reaches the
        coordinator log, never the public response — it must not carry an
        account identifier.
        """
        ...


# The production registry is code-owned (PRD FR-001): a provider enters by a
# reviewed code change, never by configuration or request. It deliberately
# stays empty in this broker-neutral package — the live registry is
# app/broker/fleet_composition.py's production_provider_adapters(), which
# composes the concrete adapters (Alpaca, as of Phase 2) the application
# actually serves.
PRODUCTION_PROVIDER_ADAPTERS: Mapping[str, BrokerProviderAdapter] = {}


def require_adapter(
    adapters: Mapping[str, BrokerProviderAdapter], provider_id: str
) -> BrokerProviderAdapter:
    """Resolve one adapter from a deployment-owned mapping, failing closed.

    The one refusal construction every lookup shares, so the coordinator's
    injected set and the production registry refuse identically. The mapping
    key must equal the adapter's own immutable ``provider_id``: registering a
    Tradier adapter under ``"alpaca"`` would provision Alpaca-labelled clerks
    with Tradier canonicalization, collapsing the provider boundary.
    """
    adapter = adapters.get(provider_id)
    if adapter is None:
        raise BrokerNotSupported(
            f"No provider adapter is registered for {provider_id!r} in this deployment.",
            next_step="Use a provider this deployment supports; adding one is a "
            "reviewed code change, not a request parameter.",
        )
    if adapter.provider_id != provider_id:
        raise BrokerNotSupported(
            f"The adapter registered for {provider_id!r} declares provider "
            f"{adapter.provider_id!r}; a provider may only be registered under its "
            "own identity.",
            next_step="Fix the deployment's adapter registration.",
        )
    return adapter


def require_protocol_compatible(
    *, reported: int | None, coordinator_version: int = FLEET_PROTOCOL_VERSION
) -> None:
    """Refuse an agent that is not provably protocol-compatible.

    Registration carries the agent's fleet protocol version, and an agent
    that reports none is exactly the older build this fence exists for: an
    unversioned caller cannot be assumed to implement the operation catalog
    or admission semantics the coordinator will advertise, so ``None``
    refuses exactly like a mismatch would.
    """
    if reported is None or reported != coordinator_version:
        raise FleetProtocolIncompatible(
            f"An agent speaking fleet protocol {reported!r} cannot register with a "
            f"coordinator speaking {coordinator_version}.",
            next_step="Run coordinator and agents from compatible builds; consult the "
            "supported mixed-version matrix.",
        )


__all__ = [
    "FLEET_PROTOCOL_VERSION",
    "PRODUCTION_PROVIDER_ADAPTERS",
    "BrokerProviderAdapter",
    "Capability",
    "OperationDrainAdmission",
    "OperationIdempotency",
    "OperationReadiness",
    "OperationStream",
    "ProviderOperation",
    "ServedContext",
    "require_adapter",
    "require_protocol_compatible",
    "validate_operation_catalog",
]
