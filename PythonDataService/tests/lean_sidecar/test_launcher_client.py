"""Unit tests for the data-plane launcher HTTP client.

Uses ``respx`` to mock the launcher's HTTP surface so these tests run
without a real launcher process. The contract under test is "the
launcher's structured 400 envelope round-trips into the right typed
exception" and "network failures surface as ``LauncherUnreachable``,
not generic httpx errors".
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.lean_sidecar.launcher.models import LaunchRequest, LaunchResponse
from app.lean_sidecar.launcher_client import (
    DEFAULT_LAUNCHER_URL,
    LauncherProtocolError,
    LauncherRejected,
    LauncherUnreachable,
    post_launch,
)

pytestmark = pytest.mark.asyncio


DUMMY_DIGEST = "sha256:00000000000000000000000000000000000000000000000000000000deadbeef"


def _request() -> LaunchRequest:
    return LaunchRequest(
        run_id="client_unit",
        image_digest=DUMMY_DIGEST,
        cpus=2.0,
        memory_mb=1024,
        pids_limit=256,
        wall_clock_timeout_s=60,
        workspace_max_mb=256,
        log_tail_bytes=4096,
    )


def _success_body() -> dict:
    return LaunchResponse(
        run_id="client_unit",
        exit_code=0,
        duration_ms=2345,
        timed_out=False,
        log_tail="ok",
        lean_errors={},
        is_clean=True,
    ).model_dump()


class TestPostLaunch:
    async def test_success_round_trips_response(self) -> None:
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            mock.post("/launch").mock(return_value=httpx.Response(200, json=_success_body()))
            response = await post_launch(_request())
        assert response.is_clean is True
        assert response.run_id == "client_unit"
        assert response.exit_code == 0

    async def test_400_envelope_becomes_typed_exception(self) -> None:
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            mock.post("/launch").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "detail": {
                            "reason": "workspace_not_staged",
                            "message": "stage data before launching",
                        }
                    },
                )
            )
            with pytest.raises(LauncherRejected) as ei:
                await post_launch(_request())
        assert ei.value.reason == "workspace_not_staged"
        assert "stage data" in ei.value.message

    async def test_500_becomes_protocol_error(self) -> None:
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            mock.post("/launch").mock(return_value=httpx.Response(500, text="boom"))
            with pytest.raises(LauncherProtocolError, match="HTTP 500"):
                await post_launch(_request())

    async def test_connection_failure_becomes_unreachable(self) -> None:
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            mock.post("/launch").mock(side_effect=httpx.ConnectError("connection refused"))
            with pytest.raises(LauncherUnreachable):
                await post_launch(_request())

    async def test_bad_envelope_falls_back_to_unknown(self) -> None:
        """A misbehaving launcher returning a non-{reason,message} 400
        body must still surface as LauncherRejected — not a crash —
        with ``reason="unknown"`` AND the raw body in ``message`` so
        the operator sees the literal text in their logs.
        """
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            mock.post("/launch").mock(return_value=httpx.Response(400, text="raw error"))
            with pytest.raises(LauncherRejected) as ei:
                await post_launch(_request())
        assert ei.value.reason == "unknown"
        assert "raw error" in ei.value.message

    async def test_token_attached_when_env_var_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAN_LAUNCHER_TOKEN", "shared-secret-abc")
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            route = mock.post("/launch").mock(return_value=httpx.Response(200, json=_success_body()))
            await post_launch(_request())
        sent = route.calls[0].request
        assert sent.headers.get("X-Launcher-Token") == "shared-secret-abc"
