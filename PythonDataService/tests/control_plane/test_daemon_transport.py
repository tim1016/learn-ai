"""PRD #619-C1 — typed daemon transport ADT + factory constructors.

The foundation is intentionally tiny: a closed-kind ``DaemonResult``
Pydantic model + a handful of factory ``@classmethod`` constructors that
the caller in 619-C2 picks at each branch of its ``try / except`` ladder.
The classification "logic" lives at the call site (where the caller
already knows which exception fired, what status came back, etc.) — not
in this module.

Tests cover:

- ``from_httpx_exception`` mapping the httpx exception hierarchy to a
  short category code + the conservative ``outcome_ambiguous`` bit
  (connect-time failures unambiguous; read/write/protocol failures
  ambiguous).
- Each factory builds the right kind + carries through the operator
  detail safely.
- ``safe_detail`` truncation + control-char folding.
- The model contract: frozen, ``extra="forbid"``, detail max-length,
  closed-kind union (including ``RETRYING`` which the per-call
  factories never emit but which the monitor in 619-C2 will fold to).
"""

from __future__ import annotations

import httpx
import pytest

from app.engine.live.daemon_transport import (
    MAX_DETAIL_LEN,
    DaemonResult,
    safe_detail,
)

# ---------------------------------------------------------------------------
# safe_detail helper
# ---------------------------------------------------------------------------


def test_safe_detail_none_passes_through() -> None:
    assert safe_detail(None) is None


def test_safe_detail_blank_returns_none() -> None:
    assert safe_detail("   ") is None
    assert safe_detail("\n\t  \r") is None


def test_safe_detail_collapses_whitespace_and_control_chars() -> None:
    raw = "line1\nline2\twith\x00null\rand spaces"

    assert safe_detail(raw) == "line1 line2 with null and spaces"


def test_safe_detail_truncates_with_ellipsis() -> None:
    raw = "x" * (MAX_DETAIL_LEN + 50)

    result = safe_detail(raw)

    assert result is not None
    assert len(result) == MAX_DETAIL_LEN
    assert result.endswith("…")


# ---------------------------------------------------------------------------
# from_httpx_exception — the one piece of genuinely module-local logic
# ---------------------------------------------------------------------------


def test_connect_error_is_unambiguous_unreachable() -> None:
    result = DaemonResult.from_httpx_exception(httpx.ConnectError("refused"))

    assert result.kind == "UNREACHABLE"
    assert result.error_category == "connect_error"
    assert result.outcome_ambiguous is False
    assert result.response_status is None


def test_connect_timeout_is_unambiguous_unreachable() -> None:
    result = DaemonResult.from_httpx_exception(httpx.ConnectTimeout("dial"))

    assert result.kind == "UNREACHABLE"
    assert result.error_category == "connect_timeout"
    assert result.outcome_ambiguous is False


def test_pool_timeout_is_unambiguous_unreachable() -> None:
    result = DaemonResult.from_httpx_exception(httpx.PoolTimeout("pool"))

    assert result.kind == "UNREACHABLE"
    assert result.error_category == "pool_timeout"
    assert result.outcome_ambiguous is False


def test_write_timeout_is_ambiguous_unreachable() -> None:
    result = DaemonResult.from_httpx_exception(httpx.WriteTimeout("write"))

    assert result.kind == "UNREACHABLE"
    assert result.error_category == "write_timeout"
    assert result.outcome_ambiguous is True


def test_read_timeout_is_ambiguous_unreachable() -> None:
    result = DaemonResult.from_httpx_exception(httpx.ReadTimeout("read"))

    assert result.kind == "UNREACHABLE"
    assert result.error_category == "read_timeout"
    assert result.outcome_ambiguous is True


def test_remote_protocol_error_is_ambiguous_unreachable() -> None:
    result = DaemonResult.from_httpx_exception(
        httpx.RemoteProtocolError("bad framing")
    )

    assert result.kind == "UNREACHABLE"
    assert result.error_category == "remote_protocol_error"
    assert result.outcome_ambiguous is True


def test_generic_network_error_is_ambiguous_unreachable() -> None:
    result = DaemonResult.from_httpx_exception(httpx.NetworkError("network"))

    assert result.kind == "UNREACHABLE"
    assert result.error_category == "network_error"
    assert result.outcome_ambiguous is True


def test_non_httpx_exception_classifies_as_transport_error() -> None:
    result = DaemonResult.from_httpx_exception(RuntimeError("unexpected"))

    assert result.kind == "UNREACHABLE"
    assert result.error_category == "transport_error"
    assert result.outcome_ambiguous is True


def test_exception_detail_is_sanitised() -> None:
    result = DaemonResult.from_httpx_exception(
        httpx.ConnectError("ugly\n\tdetail\x00bytes")
    )

    assert result.detail is not None
    assert "\n" not in result.detail
    assert "\x00" not in result.detail


def test_from_httpx_exception_accepts_optional_response_status() -> None:
    # A retrying client may surface a terminal status alongside a final
    # exception; the field is accepted for completeness.
    result = DaemonResult.from_httpx_exception(
        httpx.ReadTimeout("read"), response_status=504
    )

    assert result.kind == "UNREACHABLE"
    assert result.response_status == 504


# ---------------------------------------------------------------------------
# auth_failed / protocol_error / malformed_body / incompatible_contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failed_factory(status: int) -> None:
    result = DaemonResult.auth_failed(status=status, detail="token invalid")

    assert result.kind == "AUTH_FAILED"
    assert result.error_category == "auth_failed"
    assert result.response_status == status
    assert result.detail == "token invalid"
    assert result.outcome_ambiguous is False


@pytest.mark.parametrize("status", [400, 404, 409, 422, 500, 502, 503, 504])
def test_protocol_error_factory(status: int) -> None:
    result = DaemonResult.protocol_error(status=status, detail="downstream broken")

    assert result.kind == "PROTOCOL_ERROR"
    assert result.error_category == f"http_{status}"
    assert result.response_status == status
    assert result.detail == "downstream broken"


def test_protocol_error_without_detail_carries_no_detail() -> None:
    result = DaemonResult.protocol_error(status=500)

    assert result.kind == "PROTOCOL_ERROR"
    assert result.detail is None


def test_malformed_body_factory() -> None:
    # The caller invokes this when ``ValueError`` (json decode) fires —
    # it knows it's JSON-decode, not pydantic-validation, because the
    # exception type itself is the signal.
    result = DaemonResult.malformed_body(detail="Expecting value: line 1 column 1")

    assert result.kind == "PROTOCOL_ERROR"
    assert result.error_category == "malformed_body"
    assert result.response_status == 200
    assert result.detail is not None
    assert "Expecting value" in result.detail


def test_incompatible_contract_factory() -> None:
    # Likewise invoked on pydantic.ValidationError or a known
    # api_version skew the caller decides not to consume.
    result = DaemonResult.incompatible_contract(
        detail="1 validation error for HostRunnerHealth: field required"
    )

    assert result.kind == "INCOMPATIBLE_CONTRACT"
    assert result.error_category == "schema_mismatch"
    assert result.response_status == 200
    assert result.detail is not None
    assert "validation error" in result.detail.lower()


# ---------------------------------------------------------------------------
# connected
# ---------------------------------------------------------------------------


def test_connected_factory_default_fields() -> None:
    result = DaemonResult.connected()

    assert result.kind == "CONNECTED"
    assert result.response_status == 200
    assert result.observed_daemon_boot_id is None
    assert result.observed_daemon_api_version is None
    assert result.detail is None
    assert result.outcome_ambiguous is False


def test_connected_factory_forwards_daemon_identity() -> None:
    result = DaemonResult.connected(
        daemon_boot_id="boot-deadbeef", daemon_api_version=1
    )

    assert result.kind == "CONNECTED"
    assert result.observed_daemon_boot_id == "boot-deadbeef"
    assert result.observed_daemon_api_version == 1


def test_connected_factory_coerces_empty_boot_id_to_none() -> None:
    # A misconfigured daemon that hands back ``""`` should be treated
    # the same as a daemon that omits the field — neither is a usable
    # identity.
    result = DaemonResult.connected(daemon_boot_id="")

    assert result.kind == "CONNECTED"
    assert result.observed_daemon_boot_id is None


# ---------------------------------------------------------------------------
# Model contract
# ---------------------------------------------------------------------------


def test_daemon_result_is_frozen() -> None:
    result = DaemonResult(kind="CONNECTED")

    with pytest.raises(Exception):
        result.kind = "UNREACHABLE"  # type: ignore[misc]


def test_daemon_result_rejects_unknown_kind() -> None:
    with pytest.raises(Exception):
        DaemonResult(kind="WHATEVER")  # type: ignore[arg-type]


def test_daemon_result_rejects_extra_fields() -> None:
    with pytest.raises(Exception):
        DaemonResult(kind="CONNECTED", surprise="surprise")  # type: ignore[call-arg]


def test_daemon_result_detail_max_length_enforced_by_model() -> None:
    over = "x" * (MAX_DETAIL_LEN + 1)

    with pytest.raises(Exception):
        DaemonResult(kind="PROTOCOL_ERROR", detail=over)


def test_retrying_kind_round_trips_even_though_factories_never_emit_it() -> None:
    # The factories never emit RETRYING — that's the monitor's job
    # (619-C2). The ADT itself must still accept the kind so
    # monitor-folded values round-trip cleanly.
    result = DaemonResult(
        kind="RETRYING",
        error_category="connect_error",
        outcome_ambiguous=False,
    )

    assert result.kind == "RETRYING"


def test_no_factory_emits_retrying() -> None:
    # Sanity sweep across every factory the caller will use in 619-C2.
    cases = [
        DaemonResult.from_httpx_exception(httpx.ConnectError("x")),
        DaemonResult.from_httpx_exception(httpx.ReadTimeout("x")),
        DaemonResult.auth_failed(status=401, detail="d"),
        DaemonResult.protocol_error(status=500, detail="d"),
        DaemonResult.malformed_body(detail="bad json"),
        DaemonResult.incompatible_contract(detail="bad shape"),
        DaemonResult.connected(daemon_boot_id="boot", daemon_api_version=1),
    ]

    assert all(r.kind != "RETRYING" for r in cases)
