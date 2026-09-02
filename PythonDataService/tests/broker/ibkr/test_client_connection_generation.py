"""connection_generation increments once per successful connect (spec §4.2 rule 1)."""

from __future__ import annotations

import pytest

from app.broker.ibkr import client as client_module
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.config import IbkrSettings


class _FakeIbClient:
    def serverVersion(self) -> int:
        return 178


class _FakeIb:
    def __init__(self) -> None:
        self.client = _FakeIbClient()
        self._connected = False

    async def connectAsync(self, **kwargs) -> None:
        self._connected = True

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def managedAccounts(self) -> list[str]:
        return ["DU123456"]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> IbkrClient:
    monkeypatch.setattr(client_module, "apply_tcp_keepalive", lambda ib: None)
    # Settings are pinned rather than read from the environment so the test is
    # hermetic (``_env_file=None``) and the failed-connect case does not spend
    # ~11s in the retry backoff (``connect_attempts=1``).
    settings = IbkrSettings(
        mode="paper", host="127.0.0.1", port=4002, connect_attempts=1, _env_file=None
    )
    instance = IbkrClient(settings)
    monkeypatch.setattr(instance, "_ib", _FakeIb())
    return instance


async def test_connection_generation_starts_at_zero(client: IbkrClient) -> None:
    assert client.connection_generation == 0


async def test_connection_generation_increments_per_successful_connect(client: IbkrClient) -> None:
    await client.connect()
    assert client.connection_generation == 1

    await client.disconnect()
    await client.connect()
    assert client.connection_generation == 2


async def test_connection_generation_unchanged_on_failed_connect(
    client: IbkrClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _refuse(**kwargs) -> None:
        raise OSError("refused")

    monkeypatch.setattr(client._ib, "connectAsync", _refuse)
    with pytest.raises(Exception):
        await client.connect()
    assert client.connection_generation == 0
