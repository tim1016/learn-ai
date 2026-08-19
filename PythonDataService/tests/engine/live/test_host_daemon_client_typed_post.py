from __future__ import annotations

import httpx
import pytest
import respx

from app.engine.live.host_daemon_client import (
    HostDaemonOutcomeUnknownError,
    renew_control_plane_lease,
)

BASE = "http://daemon-host:8765"


@pytest.mark.asyncio
@respx.mock
async def test_host_capability_lease_renewal_posts_no_account_command() -> None:
    route = respx.post(f"{BASE}/control-plane/renew-lease").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = await renew_control_plane_lease(BASE)

    assert result == {"ok": True}
    assert route.calls.last.request.content == b"{}"


@pytest.mark.asyncio
@respx.mock
async def test_ambiguous_lease_renewal_transport_stays_outcome_unknown() -> None:
    respx.post(f"{BASE}/control-plane/renew-lease").mock(
        side_effect=httpx.ReadTimeout("lost response")
    )

    with pytest.raises(HostDaemonOutcomeUnknownError):
        await renew_control_plane_lease(BASE)
