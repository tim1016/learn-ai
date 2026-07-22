"""PRD #619-C5 — typed POST + ambiguous-outcome classification.

Exercises the four mutation forwarders (``deploy`` / ``start_run`` /
``stop_run`` / ``emergency_flatten_run``) end-to-end through the typed
POST core. Mocks the daemon HTTP layer with respx so the real
``httpx.AsyncClient`` exercises the request/response path.

The 619-C5 contract:

- ``CONNECTED`` (2xx + parseable dict body) → return the body.
- ``UNREACHABLE`` with ``outcome_ambiguous=True`` (ReadTimeout /
  WriteTimeout / RemoteProtocolError after bytes may have left) →
  :class:`HostDaemonOutcomeUnknownError`. The router translates this
  into a typed 409 + ``OUTCOME_UNKNOWN`` body.
- ``UNREACHABLE`` with ``outcome_ambiguous=False`` (ConnectError /
  ConnectTimeout / PoolTimeout — no bytes left) →
  :class:`HostDaemonError(503, ...)`. Retry is safe.
- 4xx / 5xx daemon-authored status → :class:`HostDaemonError(<status>,
  detail)` (verbatim).
- Malformed JSON / non-dict body → :class:`HostDaemonError(502, ...)`.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.engine.live import host_daemon_client
from app.engine.live.host_daemon_client import (
    HostDaemonError,
    HostDaemonOutcomeUnknownError,
    deploy,
    emergency_flatten_run,
    ensure_account_clerk,
    release_account_clerk,
    retire_account_binding,
    start_run,
    stop_run,
)

BASE = "http://daemon-host:8765"


# ---------------------------------------------------------------------------
# Happy path — CONNECTED returns the parsed body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_deploy_returns_parsed_body_on_2xx() -> None:
    respx.post(f"{BASE}/deploy").mock(
        return_value=httpx.Response(
            201, json={"run_id": "run-A", "run_dir": "/r/A", "created": True}
        )
    )

    result = await deploy(BASE, {"strategy_spec_path": "s"})

    assert result == {"run_id": "run-A", "run_dir": "/r/A", "created": True}


@pytest.mark.asyncio
@respx.mock
async def test_start_run_returns_parsed_body_on_2xx() -> None:
    respx.post(f"{BASE}/runs/run-A/start").mock(
        return_value=httpx.Response(200, json={"accepted": True, "process": {}})
    )

    result = await start_run(BASE, "run-A", {})

    assert result["accepted"] is True


@pytest.mark.asyncio
async def test_start_run_uses_bounded_admission_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeout: list[httpx.Timeout] = []

    async def post(_url: str, _payload: dict, *, timeout: httpx.Timeout) -> dict:
        observed_timeout.append(timeout)
        return {"accepted": True}

    monkeypatch.setattr(host_daemon_client, "_post_action", post)

    await start_run(BASE, "run-A", {})

    assert observed_timeout == [host_daemon_client._START_ADMISSION_TIMEOUT]
    assert observed_timeout[0].read > host_daemon_client._TIMEOUT.read


@pytest.mark.asyncio
@respx.mock
async def test_ensure_account_clerk_sends_host_side_broker_address() -> None:
    route = respx.post(f"{BASE}/accounts/DU123/clerk/ensure").mock(
        return_value=httpx.Response(200, json={"clerks": []})
    )

    result = await ensure_account_clerk(
        BASE,
        "DU123",
        ibkr_host="127.0.0.1",
    )

    assert result == {"clerks": []}
    assert route.calls.last.request.content == b'{"ibkr_host":"127.0.0.1"}'


@pytest.mark.asyncio
async def test_ensure_account_clerk_uses_bounded_admission_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeout: list[httpx.Timeout] = []

    async def post(_url: str, _payload: dict, *, timeout: httpx.Timeout) -> dict:
        observed_timeout.append(timeout)
        return {"clerks": []}

    monkeypatch.setattr(host_daemon_client, "_post_action", post)

    await ensure_account_clerk(BASE, "DU123")

    assert observed_timeout == [host_daemon_client._START_ADMISSION_TIMEOUT]
    assert observed_timeout[0].read > host_daemon_client._TIMEOUT.read


@pytest.mark.asyncio
async def test_release_account_clerk_uses_timeout_beyond_daemon_shutdown_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeout: list[httpx.Timeout] = []

    async def post(_url: str, _payload: dict, *, timeout: httpx.Timeout) -> dict:
        observed_timeout.append(timeout)
        return {}

    monkeypatch.setattr(host_daemon_client, "_post_action", post)

    await release_account_clerk(BASE, "DU123")

    assert observed_timeout[0].read is not None
    assert observed_timeout[0].read > 4.0


@pytest.mark.asyncio
@respx.mock
async def test_retire_account_binding_returns_a_validated_receipt() -> None:
    respx.post(f"{BASE}/accounts/DU123/bindings/retire").mock(
        return_value=httpx.Response(
            200,
            json={
                "account_id": "DU123",
                "strategy_instance_id": "stale-bot",
                "run_id": "stale-run",
                "bot_order_namespace": "learn-ai/stale-bot/v1",
                "lifecycle_state": "RETIRED",
                "recorded_at_ms": 100,
                "source": "operator.stale_binding_retirement",
            },
        )
    )

    receipt = await retire_account_binding(
        BASE,
        "DU123",
        {"strategy_instance_id": "stale-bot", "run_id": "stale-run"},
    )

    assert receipt.lifecycle_state == "RETIRED"
    assert receipt.run_id == "stale-run"


@pytest.mark.asyncio
@respx.mock
async def test_retire_account_binding_rejects_an_invalid_host_receipt() -> None:
    respx.post(f"{BASE}/accounts/DU123/bindings/retire").mock(
        return_value=httpx.Response(200, json={"not": "a binding"})
    )

    with pytest.raises(HostDaemonError) as exc_info:
        await retire_account_binding(
            BASE,
            "DU123",
            {"strategy_instance_id": "stale-bot", "run_id": "stale-run"},
        )

    assert exc_info.value.status_code == 502


# ---------------------------------------------------------------------------
# OUTCOME_UNKNOWN — ambiguous transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_read_timeout_raises_outcome_unknown() -> None:
    """ReadTimeout means the request was fully sent but the response
    never arrived — the daemon may or may not have observed it."""
    respx.post(f"{BASE}/runs/run-A/start").mock(side_effect=httpx.ReadTimeout("read"))

    with pytest.raises(HostDaemonOutcomeUnknownError) as exc_info:
        await start_run(BASE, "run-A", {})

    assert exc_info.value.error_category == "read_timeout"


@pytest.mark.asyncio
@respx.mock
async def test_write_timeout_raises_outcome_unknown() -> None:
    """WriteTimeout means the connection opened but the write timed out —
    a partial body may have reached the daemon."""
    respx.post(f"{BASE}/deploy").mock(side_effect=httpx.WriteTimeout("write"))

    with pytest.raises(HostDaemonOutcomeUnknownError) as exc_info:
        await deploy(BASE, {})

    assert exc_info.value.error_category == "write_timeout"


@pytest.mark.asyncio
@respx.mock
async def test_remote_protocol_error_raises_outcome_unknown() -> None:
    """RemoteProtocolError means HTTP framing fell over mid-exchange —
    bytes may have been observed."""
    respx.post(f"{BASE}/runs/run-A/stop").mock(
        side_effect=httpx.RemoteProtocolError("bad framing")
    )

    with pytest.raises(HostDaemonOutcomeUnknownError) as exc_info:
        await stop_run(BASE, "run-A", {})

    assert exc_info.value.error_category == "remote_protocol_error"


@pytest.mark.asyncio
@respx.mock
async def test_emergency_flatten_read_timeout_raises_outcome_unknown() -> None:
    """The 130s flatten timeout is the most common place a ReadTimeout
    fires; the broker round-trip means an ambiguous outcome here has
    the highest stakes."""
    respx.post(f"{BASE}/runs/run-A/emergency-flatten").mock(
        side_effect=httpx.ReadTimeout("read")
    )

    with pytest.raises(HostDaemonOutcomeUnknownError):
        await emergency_flatten_run(BASE, "run-A", {})


# ---------------------------------------------------------------------------
# 503 — unambiguous unreachable (clean pre-send failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_connect_error_raises_503_host_daemon_error() -> None:
    """ConnectError means the TCP connection never opened — no bytes
    left the wire, retry is safe, surface as 503 to match the
    pre-619-C5 contract."""
    respx.post(f"{BASE}/deploy").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(HostDaemonError) as exc_info:
        await deploy(BASE, {})

    assert exc_info.value.status_code == 503
    # Must NOT be classified as outcome-unknown.
    assert not isinstance(exc_info.value, HostDaemonOutcomeUnknownError)


@pytest.mark.asyncio
@respx.mock
async def test_connect_timeout_raises_503_host_daemon_error() -> None:
    respx.post(f"{BASE}/runs/run-A/start").mock(
        side_effect=httpx.ConnectTimeout("dial")
    )

    with pytest.raises(HostDaemonError) as exc_info:
        await start_run(BASE, "run-A", {})

    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Daemon-authored statuses propagate verbatim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500])
@pytest.mark.asyncio
@respx.mock
async def test_4xx_5xx_propagate_with_daemon_status(status: int) -> None:
    """Daemon-authored statuses (dirty-tree 409, missing-spec 400,
    auth 401, etc.) reach the caller verbatim — the operation
    deterministically did not occur, so OUTCOME_UNKNOWN does not apply."""
    respx.post(f"{BASE}/deploy").mock(
        return_value=httpx.Response(status, json={"detail": "daemon says no"})
    )

    with pytest.raises(HostDaemonError) as exc_info:
        await deploy(BASE, {})

    assert exc_info.value.status_code == status
    assert exc_info.value.detail == "daemon says no"


@pytest.mark.asyncio
@respx.mock
async def test_structured_daemon_error_detail_is_preserved() -> None:
    detail = {
        "reason_code": "STOPPED_REQUIRES_RESUME",
        "message": "spy_ema_paper is durably STOPPED.",
        "remediation": "Use Resume to set desired_state=RUNNING, then start the bot.",
        "gate_id": "desired_state.start",
    }
    respx.post(f"{BASE}/runs/run-A/start").mock(
        return_value=httpx.Response(409, json={"detail": detail})
    )

    with pytest.raises(HostDaemonError) as exc_info:
        await start_run(BASE, "run-A", {})

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == detail


# ---------------------------------------------------------------------------
# Malformed responses → 502
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_malformed_json_returns_502_host_daemon_error() -> None:
    respx.post(f"{BASE}/deploy").mock(
        return_value=httpx.Response(200, content=b"not-json-at-all")
    )

    with pytest.raises(HostDaemonError) as exc_info:
        await deploy(BASE, {})

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
@respx.mock
async def test_non_dict_payload_returns_502_host_daemon_error() -> None:
    respx.post(f"{BASE}/deploy").mock(
        return_value=httpx.Response(200, json=["not", "a", "dict"])
    )

    with pytest.raises(HostDaemonError) as exc_info:
        await deploy(BASE, {})

    assert exc_info.value.status_code == 502


# ---------------------------------------------------------------------------
# HostDaemonOutcomeUnknownError shape
# ---------------------------------------------------------------------------


def test_outcome_unknown_error_carries_typed_fields() -> None:
    exc = host_daemon_client.HostDaemonOutcomeUnknownError(
        error_category="read_timeout", detail="response lost"
    )

    assert exc.error_category == "read_timeout"
    assert exc.detail == "response lost"
    assert str(exc) == "response lost"


def test_outcome_unknown_error_falls_back_to_category_when_detail_missing() -> None:
    exc = host_daemon_client.HostDaemonOutcomeUnknownError(
        error_category="read_timeout", detail=None
    )

    assert str(exc) == "read_timeout"
