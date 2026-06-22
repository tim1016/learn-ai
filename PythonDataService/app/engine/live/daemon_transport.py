"""PRD #619-C foundation — typed daemon transport outcomes.

Replaces ``dict | None`` daemon reads with a closed ADT describing every
HTTP exchange between the data plane and the host live-runner daemon.

The kinds are:

- ``CONNECTED`` — daemon answered with a valid, schema-compatible body.
- ``RETRYING`` — derived monitor state (consecutive failures within
  budget). Never produced here per call; the connectivity monitor
  (619-C2) is the only thing that may fold ``UNREACHABLE`` into
  ``RETRYING``.
- ``UNREACHABLE`` — connection refused, DNS failure, or timeout. Whether
  bytes left the wire is recorded in ``outcome_ambiguous``.
- ``AUTH_FAILED`` — daemon answered 401/403.
- ``PROTOCOL_ERROR`` — daemon responded but the body could not be parsed
  or the status indicates a transport-layer fault (5xx, malformed JSON,
  gateway timeouts).
- ``INCOMPATIBLE_CONTRACT`` — schema-shape parse failure on the
  response, or a daemon-declared contract version the data plane cannot
  consume.

The foundation in this module is intentionally tiny: the ``DaemonResult``
model + the one piece of genuinely module-local logic
(``from_httpx_exception`` — mapping the httpx exception hierarchy to a
short category code and the conservative ``outcome_ambiguous`` bit) +
trivial factory constructors. The caller's own ``try / except`` ladder
at the call site (wired up in 619-C2) is the classification:

    try:
        response = await client.get(url)
    except Exception as exc:
        return DaemonResult.from_httpx_exception(exc)
    if response.status_code in (401, 403):
        return DaemonResult.auth_failed(detail=...)
    if response.status_code >= 400:
        return DaemonResult.protocol_error(status=response.status_code,
                                           detail=...)
    try:
        payload = HostRunnerHealth.model_validate_json(response.content)
    except pydantic.ValidationError as exc:
        return DaemonResult.incompatible_contract(detail=str(exc))
    except ValueError as exc:
        return DaemonResult.malformed_body(detail=str(exc))
    return DaemonResult.connected(payload)

By keeping the branching at the call site rather than centralising it in
a multi-kwarg classifier, the foundation avoids: a brittle string-pattern
heuristic to distinguish malformed-JSON from schema-mismatch, an untyped
``Mapping[str, Any]`` handoff that would re-implement Pydantic
validation, and a "misuse" surface where the API can be called with
nothing meaningful and still produce a result.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

DaemonResultKind = Literal[
    "CONNECTED",
    "RETRYING",
    "UNREACHABLE",
    "AUTH_FAILED",
    "PROTOCOL_ERROR",
    "INCOMPATIBLE_CONTRACT",
]

MAX_DETAIL_LEN = 240


def safe_detail(text: str | None) -> str | None:
    """Cap and sanitise a detail string for operator display.

    Truncates to ``MAX_DETAIL_LEN`` characters (ellipsis on truncation),
    folds tabs / newlines / control bytes to a single space, and
    collapses runs of whitespace. Returns ``None`` for empty/blank input
    so the operator UI can hide the field.
    """
    if text is None:
        return None
    cleaned = "".join(
        " " if (c.isspace() or (ord(c) < 0x20)) else c for c in text
    ).strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    if len(cleaned) > MAX_DETAIL_LEN:
        return cleaned[: MAX_DETAIL_LEN - 1].rstrip() + "…"
    return cleaned


def _exception_category(exc: BaseException) -> tuple[str, bool]:
    """Map an exception (httpx or otherwise) to ``(category, ambiguous)``.

    ``outcome_ambiguous`` is True only for failures that left in-flight
    request bytes potentially observed by the daemon. ``ConnectError`` /
    ``ConnectTimeout`` / ``PoolTimeout`` are unambiguous — the TCP
    connection never opened, so no bytes were sent. ``ReadTimeout`` /
    ``WriteTimeout`` / ``RemoteProtocolError`` are ambiguous — the
    daemon may have observed the request.

    Order matters: ``ConnectTimeout`` is a subclass of both
    ``ConnectError`` and ``TimeoutException``; classify the more specific
    cases first.
    """
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout", False
    if isinstance(exc, httpx.ConnectError):
        return "connect_error", False
    if isinstance(exc, httpx.WriteTimeout):
        return "write_timeout", True
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout", True
    if isinstance(exc, httpx.PoolTimeout):
        return "pool_timeout", False
    if isinstance(exc, httpx.RemoteProtocolError):
        return "remote_protocol_error", True
    if isinstance(exc, httpx.NetworkError):
        return "network_error", True
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", True
    return "transport_error", True


class DaemonResult(BaseModel):
    """Per-call typed outcome of one daemon HTTP exchange.

    The union is closed by ``kind``. Per-call factories never produce
    ``RETRYING`` — that label belongs to the connectivity monitor in
    619-C2, which folds repeated ``UNREACHABLE`` outcomes into
    ``RETRYING`` while attempts remain under budget. The model still
    accepts the kind so monitor-folded values round-trip cleanly.

    Field semantics:

    - ``error_category`` is a short stable code (``"connect_error"``,
      ``"read_timeout"``, ``"schema_mismatch"`` …) suitable for runbook
      lookup. Distinct from ``detail`` which is a human-readable
      one-liner.
    - ``outcome_ambiguous`` is True only when transmission could not be
      proven non-occurring — i.e., ``ReadTimeout``/``WriteTimeout``/
      ``RemoteProtocolError`` after the connection was established.
      Mutation callers consult this bit to surface ``OUTCOME_UNKNOWN``
      to the operator (619-C5 / 619-D).
    - ``response_status`` is populated whenever the daemon returned an
      HTTP response, regardless of kind.
    - ``observed_daemon_boot_id`` / ``observed_daemon_api_version`` are
      forwarded from the parsed ``HostRunnerHealth`` payload by the
      ``connected()`` factory. The monitor uses these to detect boot_id
      changes (619-B / 619-C2).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DaemonResultKind
    detail: str | None = Field(default=None, max_length=MAX_DETAIL_LEN)
    error_category: str | None = Field(default=None, max_length=64)
    outcome_ambiguous: bool = False
    response_status: int | None = Field(default=None, ge=100, le=599)
    observed_daemon_boot_id: str | None = None
    observed_daemon_api_version: int | None = Field(default=None, ge=1)

    # ------------------------------------------------------------------
    # Factories — each branch of the caller's classification picks one.
    # ------------------------------------------------------------------

    @classmethod
    def from_httpx_exception(
        cls,
        exc: BaseException,
        *,
        response_status: int | None = None,
    ) -> DaemonResult:
        """Build an ``UNREACHABLE`` result from a transport-layer exception.

        ``response_status`` is accepted for completeness (e.g. when a
        retrying client surfaces a status alongside a terminal exception),
        but is normally ``None`` — by definition, an exception means the
        exchange did not complete.
        """
        category, ambiguous = _exception_category(exc)
        return cls(
            kind="UNREACHABLE",
            detail=safe_detail(str(exc) or category),
            error_category=category,
            outcome_ambiguous=ambiguous,
            response_status=response_status,
        )

    @classmethod
    def auth_failed(
        cls, *, status: int, detail: str | None = None
    ) -> DaemonResult:
        """Build an ``AUTH_FAILED`` result for a 401/403 response."""
        return cls(
            kind="AUTH_FAILED",
            detail=safe_detail(detail),
            error_category="auth_failed",
            response_status=status,
        )

    @classmethod
    def protocol_error(
        cls, *, status: int, detail: str | None = None
    ) -> DaemonResult:
        """Build a ``PROTOCOL_ERROR`` result for a non-auth 4xx/5xx response."""
        return cls(
            kind="PROTOCOL_ERROR",
            detail=safe_detail(detail),
            error_category=f"http_{status}",
            response_status=status,
        )

    @classmethod
    def malformed_body(
        cls, *, status: int = 200, detail: str | None = None
    ) -> DaemonResult:
        """Build a ``PROTOCOL_ERROR`` result for a JSON-decode failure."""
        return cls(
            kind="PROTOCOL_ERROR",
            detail=safe_detail(detail),
            error_category="malformed_body",
            response_status=status,
        )

    @classmethod
    def incompatible_contract(
        cls, *, status: int = 200, detail: str | None = None
    ) -> DaemonResult:
        """Build an ``INCOMPATIBLE_CONTRACT`` result for a schema mismatch.

        Used by the caller when ``pydantic.ValidationError`` fires on the
        daemon response, or when a daemon-declared contract version
        cannot be consumed by the data plane. The caller chooses this
        constructor based on the typed exception, not on string
        heuristics over the error message.
        """
        return cls(
            kind="INCOMPATIBLE_CONTRACT",
            detail=safe_detail(detail),
            error_category="schema_mismatch",
            response_status=status,
        )

    @classmethod
    def connected(
        cls,
        *,
        status: int = 200,
        daemon_boot_id: str | None = None,
        daemon_api_version: int | None = None,
    ) -> DaemonResult:
        """Build a ``CONNECTED`` result from a validated daemon response.

        The caller in 619-C2 passes ``daemon_boot_id`` / ``daemon_api_version``
        directly off the typed ``HostRunnerHealth`` payload — no untyped
        dict extraction. ``None`` is the right value when the daemon
        doesn't yet declare the field (absence is not evidence of mismatch).
        """
        return cls(
            kind="CONNECTED",
            response_status=status,
            observed_daemon_boot_id=_nonempty(daemon_boot_id),
            observed_daemon_api_version=daemon_api_version,
        )


def _nonempty(value: Any) -> str | None:
    """Coerce empty-string boot_id to ``None`` so the model field stays optional.

    The daemon's ``HostRunnerHealth.daemon_boot_id`` is ``str | None``;
    an empty-string sentinel from a misconfigured daemon should be
    treated the same as missing.
    """
    if isinstance(value, str) and value:
        return value
    return None
