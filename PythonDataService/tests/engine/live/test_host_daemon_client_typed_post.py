from __future__ import annotations

import httpx
import pytest
import respx

from app.engine.live.host_daemon_client import (
    HostDaemonOutcomeUnknownError,
    ensure_account_clerk,
    operator_recovery_flatten,
)

BASE = "http://daemon-host:8765"


@pytest.mark.asyncio
@respx.mock
async def test_account_clerk_ensure_posts_only_account_capability() -> None:
    route = respx.post(f"{BASE}/accounts/DU123/clerk/ensure").mock(
        return_value=httpx.Response(200, json={"clerks": []})
    )

    result = await ensure_account_clerk(BASE, "DU123", ibkr_host="127.0.0.1")

    assert result == {"clerks": []}
    assert route.calls.last.request.content == b'{"ibkr_host":"127.0.0.1"}'


@pytest.mark.asyncio
@respx.mock
async def test_ambiguous_account_recovery_transport_stays_outcome_unknown() -> None:
    respx.post(f"{BASE}/accounts/DU123/clerk/operator-recovery-flatten").mock(
        side_effect=httpx.ReadTimeout("lost response")
    )

    with pytest.raises(HostDaemonOutcomeUnknownError):
        await operator_recovery_flatten(BASE, "DU123", {})
