"""Tests for app.broker.ibkr.client — sentinel logic and lifecycle.

The ``IB`` instance is replaced with an ``unittest.mock.MagicMock`` —
ib_async is a heavy dependency and these tests need to run on hosts
that don't have it installed (light-layer-only CI for unrelated work).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.ibkr.client import (
    BrokerError,
    ConnectionRefusedDueToSentinelError,
    IbkrClient,
    IbkrClientIdInUseError,
    NotConnectedError,
    _detect_host_gateway,
    _is_paper_account,
    _resolve_host,
    _resolve_host_alias,
)
from app.broker.ibkr.config import IbkrSettings


@pytest.fixture
def settings_paper() -> IbkrSettings:
    return IbkrSettings(mode="paper", port=4002, connect_attempts=1, _env_file=None)


@pytest.fixture
def settings_live() -> IbkrSettings:
    return IbkrSettings(mode="live", port=4001, connect_attempts=1, _env_file=None)


def _patched_ib_class() -> tuple:
    """Patch ``ib_async.IB`` with a MagicMock that returns a fresh
    AsyncMock-backed ``IB()`` per call.

    NOTE: ``IB.disconnect`` is synchronous in ib_async — pre-mock it as a
    plain ``MagicMock`` (not ``AsyncMock``) so this fixture matches the
    real API. A previous version pre-mocked ``disconnectAsync`` as
    ``AsyncMock``, which silently auto-created the wrong attribute and
    masked the 2026-05-13 disconnect bug.
    """
    fake_ib = MagicMock()
    fake_ib.connectAsync = AsyncMock(return_value=None)
    fake_ib.disconnect = MagicMock(return_value=None)
    fake_ib.isConnected = MagicMock(return_value=True)
    fake_ib.client = MagicMock()
    fake_ib.client.serverVersion = MagicMock(return_value=178)
    fake_ib.managedAccounts = MagicMock(return_value=[])
    fake_class = MagicMock(return_value=fake_ib)
    return fake_ib, fake_class


def test_paper_account_sentinel_predicate() -> None:
    assert _is_paper_account("DU1234567")
    assert _is_paper_account("du1234567")
    assert not _is_paper_account("U1234567")
    assert not _is_paper_account("F1234567")


def test_error_1101_marks_subscriptions_stale(settings_paper: IbkrSettings) -> None:
    _fake_ib, fake_class = _patched_ib_class()

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)

    client._on_ib_error(0, 1100, "lost", None)
    assert client.connection_state == "soft_lost"

    client._on_ib_error(0, 1101, "restored data lost", None)

    assert client.connection_lost is False
    assert client.subscriptions_stale is True
    assert client.connection_state == "subscriptions_stale"
    assert client.health().last_ibkr_code == 1101
    assert client.health().recovery_state == "RESTORING"
    assert client.health().subscriptions_stale is True


def test_data_farm_codes_mark_degraded_until_ok(settings_paper: IbkrSettings) -> None:
    _fake_ib, fake_class = _patched_ib_class()

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)

    client._on_ib_error(0, 2103, "market data farm disconnected", None)

    assert client.connection_state == "degraded_data_farm"
    assert client.health().recovery_state == "HEALTHY"
    assert client.health().data_farm_degraded is True

    client._on_ib_error(0, 2104, "market data farm ok", None)

    assert client.connection_state == "connected"
    assert client.health().recovery_state == "HEALTHY"
    assert client.health().data_farm_degraded is False


# ── host auto-detection (default-gateway parsing) ───────────────────────


def _write_route_file(tmp_path, body: str) -> str:
    """Helper: write a fake /proc/net/route and return its path."""
    p = tmp_path / "route"
    p.write_text(body)
    return str(p)


def test_detect_host_gateway_parses_default_route_correctly(tmp_path) -> None:
    # Real-shape /proc/net/route. Default route is line 2 below.
    # 0202000A is little-endian for 10.0.2.2 (the canonical Podman host).
    body = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0202000A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000010AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    )
    detected = _detect_host_gateway(_write_route_file(tmp_path, body))
    assert detected == "10.0.2.2"


def test_detect_host_gateway_skips_non_default_routes(tmp_path) -> None:
    # No 00000000 destination → no default route → None.
    body = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t000010AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    )
    assert _detect_host_gateway(_write_route_file(tmp_path, body)) is None


def test_detect_host_gateway_requires_rtf_gateway_flag(tmp_path) -> None:
    # 00000000 destination but flags=0x0001 (RTF_UP only, no RTF_GATEWAY) → None.
    body = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0202000A\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"
    )
    assert _detect_host_gateway(_write_route_file(tmp_path, body)) is None


def test_detect_host_gateway_returns_none_on_missing_file(tmp_path) -> None:
    assert _detect_host_gateway(tmp_path / "does-not-exist") is None


def test_resolve_host_passes_explicit_ip_through() -> None:
    assert _resolve_host("192.168.1.5") == "192.168.1.5"
    assert _resolve_host("host.docker.internal") == "host.docker.internal"
    assert _resolve_host("127.0.0.1") == "127.0.0.1"


def test_resolve_host_resolves_auto_via_route_file(monkeypatch, tmp_path) -> None:
    body = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t01001FAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    )
    route = _write_route_file(tmp_path, body)
    # No container host alias registered → must fall back to the gateway path.
    monkeypatch.setattr(
        "app.broker.ibkr.client._resolve_host_alias",
        lambda aliases=("host.containers.internal", "host.docker.internal"): None,
    )
    monkeypatch.setattr(
        "app.broker.ibkr.client._detect_host_gateway",
        lambda route_file="/proc/net/route": "172.31.0.1",
    )
    assert _resolve_host("auto") == "172.31.0.1"
    # Sanity-check the underlying helper too — call site passes a route
    # path, so the patched callable must accept the same parameter.
    from app.broker.ibkr.client import _detect_host_gateway as real_detect

    assert real_detect(route) == "172.31.0.1"


def test_resolve_host_falls_back_to_literal_when_detection_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.broker.ibkr.client._resolve_host_alias",
        lambda aliases=("host.containers.internal", "host.docker.internal"): None,
    )
    monkeypatch.setattr(
        "app.broker.ibkr.client._detect_host_gateway",
        lambda route_file="/proc/net/route": None,
    )
    assert _resolve_host("auto") == "auto"


def test_resolve_host_prefers_container_alias_over_gateway(monkeypatch) -> None:
    """The macOS Podman applehv case: the bridge gateway points at the VM, not
    the host. ``host.containers.internal`` (registered via ``extra_hosts``) is
    the only address that reaches the actual macOS host where IB Gateway runs.
    Auto-resolution must prefer the alias whenever it resolves, regardless of
    whether a gateway is also detectable.
    """
    monkeypatch.setattr(
        "app.broker.ibkr.client._resolve_host_alias",
        lambda aliases=("host.containers.internal", "host.docker.internal"): "host.containers.internal",
    )
    # Even if a gateway is detectable, the alias path wins.
    monkeypatch.setattr(
        "app.broker.ibkr.client._detect_host_gateway",
        lambda route_file="/proc/net/route": "10.89.0.1",
    )
    assert _resolve_host("auto") == "host.containers.internal"


def test_resolve_host_alias_returns_first_resolvable(monkeypatch) -> None:
    """Resolution walks aliases in preference order — the first one that
    resolves wins. Verifies the Docker-Desktop-only case where the canonical
    Podman name isn't registered but ``host.docker.internal`` is.
    """
    import socket

    def fake_gethostbyname(name: str) -> str:
        if name == "host.containers.internal":
            raise OSError("name resolution failed")
        if name == "host.docker.internal":
            return "192.168.65.2"
        raise AssertionError(f"unexpected lookup: {name}")

    monkeypatch.setattr(socket, "gethostbyname", fake_gethostbyname)
    assert _resolve_host_alias() == "host.docker.internal"


def test_resolve_host_alias_returns_none_when_no_alias_resolves(monkeypatch) -> None:
    """Bare-metal Podman with no ``extra_hosts`` registered: every alias lookup
    fails. ``_resolve_host_alias`` returns None so ``_resolve_host`` falls back
    to the gateway path instead of asserting a host the wire can't reach.
    """
    import socket

    def fake_gethostbyname(name: str) -> str:
        raise OSError("name resolution failed")

    monkeypatch.setattr(socket, "gethostbyname", fake_gethostbyname)
    assert _resolve_host_alias() is None


@pytest.mark.asyncio
async def test_connect_succeeds_when_paper_account_returned(
    settings_paper: IbkrSettings,
) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.managedAccounts.return_value = ["DU1234567"]

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
        health = await client.connect()

    assert client.connected_account == "DU1234567"
    assert health.is_paper is True
    assert health.connected is True
    fake_ib.connectAsync.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_aborts_when_paper_mode_gets_live_account(
    settings_paper: IbkrSettings,
) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.managedAccounts.return_value = ["U7654321"]

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
        with pytest.raises(ConnectionRefusedDueToSentinelError):
            await client.connect()

    fake_ib.disconnect.assert_called()
    assert client.connected_account is None


@pytest.mark.asyncio
async def test_connect_aborts_when_live_mode_gets_paper_account(
    settings_live: IbkrSettings,
) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.managedAccounts.return_value = ["DU1234567"]

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_live)
        with pytest.raises(ConnectionRefusedDueToSentinelError):
            await client.connect()


@pytest.mark.asyncio
async def test_connect_raises_when_no_managed_accounts(
    settings_paper: IbkrSettings,
) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.managedAccounts.return_value = []

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
        with pytest.raises(BrokerError):
            await client.connect()


@pytest.mark.asyncio
async def test_connect_retries_then_fails(settings_paper: IbkrSettings) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.connectAsync.side_effect = OSError("Gateway unreachable")

    settings_three = IbkrSettings(mode="paper", port=4002, connect_attempts=3, _env_file=None)

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_three)
        with pytest.raises(BrokerError):
            await client.connect()

    assert fake_ib.connectAsync.await_count == 3


@pytest.mark.asyncio
async def test_connect_fast_fails_on_client_id_in_use_no_retries() -> None:
    """TWS error 326 must short-circuit the retry loop with a typed error.

    Regression: 2026-05-14 paper dry-run hit a zombie clientId=42 after
    a prior failed run. ib_async raises only TimeoutError from
    connectAsync (the underlying error 326 is logged to the wrapper),
    so the runner burned its full 3-attempt budget — taking ~18 seconds
    — then surfaced "BrokerError: ... last error: TimeoutError()",
    which hid the actionable cause from the operator.

    Error 326 cannot be resolved by retry — the slot stays reserved
    until the zombie socket times out (minutes) or Gateway is
    restarted. So: fail on attempt 1 with IbkrClientIdInUseError,
    name the remediation in the message, and don't burn more time.
    """
    fake_ib, fake_class = _patched_ib_class()

    settings_three = IbkrSettings(mode="paper", port=4002, connect_attempts=3, _env_file=None)

    async def _connect_simulating_326(**kwargs):
        # The real ib_async fires errorEvent with code 326 from the
        # wrapper before connectAsync times out. Reproduce that
        # ordering by calling the registered handler first.
        client_under_test._on_ib_error(-1, 326, "Unable to connect as the client id is already in use.", None)
        raise TimeoutError()

    fake_ib.connectAsync.side_effect = _connect_simulating_326

    with patch("ib_async.IB", fake_class):
        client_under_test = IbkrClient(settings_three)
        with pytest.raises(IbkrClientIdInUseError) as excinfo:
            await client_under_test.connect()

    # Fast-fail: exactly ONE connect attempt, no retries.
    assert fake_ib.connectAsync.await_count == 1
    # The remediation must be in the message — that's the whole point.
    msg = str(excinfo.value)
    assert "already in use" in msg
    assert "restart IB Gateway" in msg or "IBKR_CLIENT_ID" in msg


@pytest.mark.asyncio
async def test_connect_does_not_misclassify_non_326_errors_as_client_id_in_use() -> None:
    """A non-326 errorEvent firing during a connect failure must NOT
    cause IbkrClientIdInUseError. Only code 326 triggers the fast-fail.
    """
    fake_ib, fake_class = _patched_ib_class()

    settings_one = IbkrSettings(mode="paper", port=4002, connect_attempts=1, _env_file=None)

    async def _connect_with_unrelated_error(**kwargs):
        # Simulate ib_async firing a DIFFERENT error code (e.g. 502
        # "Couldn't connect to TWS") before the timeout.
        client_under_test._on_ib_error(-1, 502, "Couldn't connect to TWS.", None)
        raise OSError("Gateway unreachable")

    fake_ib.connectAsync.side_effect = _connect_with_unrelated_error

    with patch("ib_async.IB", fake_class):
        client_under_test = IbkrClient(settings_one)
        # Should raise BrokerError (the generic case), NOT IbkrClientIdInUseError.
        with pytest.raises(BrokerError) as excinfo:
            await client_under_test.connect()

    assert not isinstance(excinfo.value, IbkrClientIdInUseError)


# ── connectivity-loss handling (errorEvent + require_live) ──────────────


def _client_with_fake_ib(settings_paper: IbkrSettings) -> IbkrClient:
    fake_ib, fake_class = _patched_ib_class()
    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
    # Keep a handle on the underlying mock so tests can flip isConnected.
    client._fake_ib = fake_ib  # type: ignore[attr-defined]
    return client


def test_on_ib_error_marks_connection_lost_on_1100(settings_paper: IbkrSettings) -> None:
    """Regression (B-04): TWS 1100 ("connectivity lost") must be captured into
    client state and counted, not dropped on the floor like every non-326 code
    used to be."""
    client = _client_with_fake_ib(settings_paper)
    assert client.connection_lost is False

    client._on_ib_error(-1, 1100, "Connectivity between IB and TWS has been lost.", None)

    assert client.connection_lost is True
    assert client.connectivity_lost_count == 1


def test_on_ib_error_clears_connection_lost_on_restore(settings_paper: IbkrSettings) -> None:
    client = _client_with_fake_ib(settings_paper)
    client._on_ib_error(-1, 1100, "lost", None)
    assert client.connection_lost is True

    client._on_ib_error(-1, 1101, "Connectivity restored - data maintained.", None)
    assert client.connection_lost is False


def test_on_ib_error_ignores_unrelated_codes(settings_paper: IbkrSettings) -> None:
    client = _client_with_fake_ib(settings_paper)
    client._on_ib_error(-1, 2104, "Market data farm connection is OK", None)
    assert client.connection_lost is False
    assert client.connectivity_lost_count == 0


def test_require_live_raises_on_soft_connectivity_loss(settings_paper: IbkrSettings) -> None:
    """A soft 1100 leaves the socket open (isConnected True) but the feed dead.
    require_live must still raise so streaming loops halt."""
    client = _client_with_fake_ib(settings_paper)
    client._fake_ib.isConnected.return_value = True  # socket still open
    client._on_ib_error(-1, 1100, "lost", None)

    with pytest.raises(NotConnectedError, match="connectivity lost"):
        client.require_live()


def test_require_live_raises_when_socket_closed(settings_paper: IbkrSettings) -> None:
    client = _client_with_fake_ib(settings_paper)
    client._fake_ib.isConnected.return_value = False

    with pytest.raises(NotConnectedError):
        client.require_live()


@pytest.mark.asyncio
async def test_disconnect_is_idempotent(settings_paper: IbkrSettings) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.isConnected.return_value = False  # already disconnected

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
        await client.disconnect()
        await client.disconnect()

    fake_ib.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_calls_sync_ib_disconnect_when_connected(
    settings_paper: IbkrSettings,
) -> None:
    # Surfaced 2026-05-13 by the paper-Gateway smoke run: ib_async.IB
    # exposes a synchronous .disconnect(), not .disconnectAsync(). The
    # other tests in this file use an unspec'd MagicMock that silently
    # auto-creates .disconnectAsync, hiding the mismatch. spec=IB
    # restricts attribute access to real IB methods.
    import ib_async

    fake_ib = MagicMock(spec=ib_async.IB)
    # errorEvent is set in IB.__init__() as an Event instance, not an
    # attribute declared at class level — spec=IB doesn't pick it up.
    # IbkrClient.__init__ subscribes a handler via "errorEvent +=
    # handler" so the attribute must exist on the spec'd mock.
    fake_ib.errorEvent = MagicMock()
    fake_ib.isConnected.return_value = True
    fake_ib.disconnect.return_value = "Disconnected"
    fake_class = MagicMock(return_value=fake_ib)

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
        client._connected_account = "DU1234567"
        await client.disconnect()

    fake_ib.disconnect.assert_called_once()
    assert client.connected_account is None


# ── connection-state machine (broker-stability hardening) ──────────────


def test_connection_state_reports_connected_on_happy_path(
    settings_paper: IbkrSettings,
) -> None:
    client = _client_with_fake_ib(settings_paper)
    client._fake_ib.isConnected.return_value = True

    assert client.connection_state == "connected"


def test_connection_state_reports_soft_lost_on_1100(settings_paper: IbkrSettings) -> None:
    """A 1100 error keeps the socket open but the feed dead — the cockpit
    must see ``soft_lost``, not ``connected``."""
    client = _client_with_fake_ib(settings_paper)
    client._fake_ib.isConnected.return_value = True
    client._on_ib_error(-1, 1100, "lost", None)

    assert client.connection_state == "soft_lost"


def test_connection_state_reports_disconnected_when_socket_closed(
    settings_paper: IbkrSettings,
) -> None:
    client = _client_with_fake_ib(settings_paper)
    client._fake_ib.isConnected.return_value = False

    assert client.connection_state == "disconnected"


def test_health_publishes_client_observed_fields_only(
    settings_paper: IbkrSettings,
) -> None:
    """``health()`` is a pure read of client-observable state.
    ``connection_state`` is one of {connected, soft_lost, disconnected};
    the monitor's "reconnecting" overlay is applied later by
    ``build_broker_health``."""
    client = _client_with_fake_ib(settings_paper)
    client._fake_ib.isConnected.return_value = True
    client._on_ib_error(-1, 1100, "lost", None)

    h = client.health()

    assert h.connection_state == "soft_lost"
    assert h.recovery_state == "LINK_INTERRUPTED"
    assert h.connection_lost is True
    assert h.connectivity_lost_count == 1
    # Monitor-owned fields default — composer fills them in.
    assert h.reconnect_attempt is None
    assert h.successful_reconnect_count == 0
    assert h.last_transition_ms > 0


def test_on_ib_error_stamps_last_event_ms_at_the_mutation_site(
    settings_paper: IbkrSettings,
) -> None:
    """The transition timestamp is event-stamped, not observed — so a
    cockpit that polls late still sees the original transition wall-clock.
    Repeated 1100s do NOT re-stamp the timestamp (already in soft_lost)."""
    client = _client_with_fake_ib(settings_paper)
    before = client.last_event_ms

    client._on_ib_error(-1, 1100, "lost", None)
    after_first = client.last_event_ms
    assert after_first >= before

    client._on_ib_error(-1, 1100, "lost again", None)
    after_second = client.last_event_ms
    # Same state — no transition, no re-stamp.
    assert after_second == after_first

    client._on_ib_error(-1, 1101, "restored", None)
    after_restore = client.last_event_ms
    assert after_restore >= after_first


# ---------------------------------------------------------------------------
# Log-level demotion (incident taxonomy PR-3, plan §4.1): "IBKR
# connectivity lost" was demoted from WARNING to INFO because IBKR
# codes 1100/504 are frequent transient blips during a healthy session
# and the auto-reconnect-monitor already surfaces the cases that
# don't recover within one tick as a WARNING-level
# BROKER_RECONNECT_FAILED.
# ---------------------------------------------------------------------------


def test_on_ib_error_connectivity_lost_logs_at_info_not_warning(
    settings_paper: IbkrSettings, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client_with_fake_ib(settings_paper)

    caplog.clear()
    with caplog.at_level("INFO", logger="app.broker.ibkr.client"):
        client._on_ib_error(-1, 1100, "lost", None)

    matching = [r for r in caplog.records if r.message == "IBKR connectivity lost"]
    assert len(matching) == 1
    assert matching[0].levelname == "INFO"
    # The structured extra must survive the demotion — the classifier's
    # exact-anchor on (logger, message) still catches the row as
    # BROKER_DISCONNECT when it does fire (manual ops, edge timing).
    assert matching[0].action == "connection_lost"
    assert matching[0].error_code == 1100
    # State machine unchanged by the demotion.
    assert client.connection_lost is True


# ---------------------------------------------------------------------------
# Rate-limit for broker-event-log write failures (codex D5). First
# failure per run logs WARNING; subsequent failures suppress the log
# but still increment the counter + stamp the timestamp. Both fields
# surface through health() so the cockpit runtime banner can render
# "evidence integrity degraded" on recurrences.
# ---------------------------------------------------------------------------


def _force_broker_event_log_write_failure(client: IbkrClient) -> None:
    """Trigger ``_record_broker_event``'s exception branch deterministically.

    Pointing ``live_runs_root`` at a path that can't be created (here:
    under a regular file, so the ``mkdir(parents=True)`` raises NotADirectoryError)
    forces an OSError on the very first write. Cheaper and more
    deterministic than a tmpdir + chmod dance.
    """
    client._record_broker_event("TEST", probe="x")


def test_record_broker_event_logs_first_failure_warning_suppresses_rest(
    settings_paper: IbkrSettings, caplog: pytest.LogCaptureFixture, tmp_path
) -> None:
    # Point the events log at a path that the writer can't create
    # (parent is a file, not a directory) so the mkdir + open chain
    # raises an OSError every call.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    settings_paper.live_runs_root = str(blocker)
    client = _client_with_fake_ib(settings_paper)

    caplog.clear()
    with caplog.at_level("WARNING", logger="app.broker.ibkr.client"):
        _force_broker_event_log_write_failure(client)
        _force_broker_event_log_write_failure(client)
        _force_broker_event_log_write_failure(client)

    # Only the first failure emitted the WARNING line; subsequent ones
    # were rate-limited away (codex D5).
    write_warnings = [
        r
        for r in caplog.records
        if r.message.startswith("Could not append IBKR broker event log")
    ]
    assert len(write_warnings) == 1
    assert write_warnings[0].levelname == "WARNING"
    assert write_warnings[0].action == "broker_event_log_write_failed"

    # The counter and timestamp still track every failure.
    assert client._broker_event_log_write_failed_count == 3
    assert client._last_broker_event_log_write_failed_at_ms is not None


def test_health_surfaces_broker_event_log_failure_counter_and_timestamp(
    settings_paper: IbkrSettings, tmp_path
) -> None:
    # Same forced-failure setup; assert the two new fields flow through
    # the health() snapshot so the cockpit runtime banner can render
    # "evidence integrity degraded" without needing a custom probe.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    settings_paper.live_runs_root = str(blocker)
    client = _client_with_fake_ib(settings_paper)

    # Baseline before any failure.
    h0 = client.health()
    assert h0.broker_event_log_write_failed_count == 0
    assert h0.last_broker_event_log_write_failed_at_ms is None

    _force_broker_event_log_write_failure(client)
    _force_broker_event_log_write_failure(client)

    h1 = client.health()
    assert h1.broker_event_log_write_failed_count == 2
    assert h1.last_broker_event_log_write_failed_at_ms is not None
    assert h1.last_broker_event_log_write_failed_at_ms >= h0.fetched_at_ms
