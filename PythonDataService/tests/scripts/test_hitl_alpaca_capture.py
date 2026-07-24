"""Regression tests for the guarded Alpaca fixture-capture operator script."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import hitl_alpaca_capture as capture


def _write_journal_entry(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(entry) + "\n")


def test_latest_journal_entry_uses_configured_root_and_current_run_only(tmp_path: Path) -> None:
    capture_root = tmp_path / "configured-capture-root"
    path = capture_root / "alpaca" / "account" / f"{capture._TODAY_UTC}.jsonl"
    _write_journal_entry(path, {"captured_at_ms": 100, "raw_body": "stale"})
    _write_journal_entry(path, {"captured_at_ms": 200, "raw_body": "current"})

    entry = capture._latest_journal_entry(
        capture_root,
        "alpaca",
        "account",
        captured_after_ms=150,
    )

    assert entry == {"captured_at_ms": 200, "raw_body": "current"}


def test_write_order_fixtures_redacts_identifiers_and_retains_only_synthetic_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = tmp_path / "fixtures"
    monkeypatch.setattr(capture, "_FIXTURE_DIR", fixture_root)
    monkeypatch.setattr(capture, "_REPO_ROOT", tmp_path)
    orders_dir = fixture_root / "orders"
    orders_dir.mkdir(parents=True)
    (orders_dir / "orders.json").write_text(
        json.dumps(
            [
                {
                    "id": "00000000-0000-0000-0000-000000000099",
                    "client_order_id": "manual/hitl-gate/v1:SYNTHETIC00000000000001",
                    "symbol": "SPY",
                }
            ]
        )
    )

    raw_order_ref = "manual/hitl-gate/v1:operator-linkable-token"
    capture._write_order_fixtures(
        [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "client_order_id": raw_order_ref,
                "symbol": "SPY",
            }
        ],
        [
            {"_meta": "auth_ack", "frame": {"stream": "authorization", "data": {}}},
            {
                "_meta": "lifecycle/fill",
                "frame": {
                    "stream": "trade_updates",
                    "data": {"event": "fill", "order": {"client_order_id": raw_order_ref}},
                },
            },
            {
                "_meta": "other_order",
                "frame": {
                    "stream": "trade_updates",
                    "data": {"event": "new", "order": {"client_order_id": "foreign-order"}},
                },
            },
        ],
        order_ref=raw_order_ref,
        captured_at_ms=1_700_000_000_000,
    )

    orders = json.loads((orders_dir / "orders.json").read_text())
    assert [order["client_order_id"] for order in orders] == [
        capture._ORDER_IDENTIFIER_SENTINEL,
        "manual/hitl-gate/v1:SYNTHETIC00000000000001",
    ]
    order_attribution = (orders_dir / "attribution.md").read_text()
    assert raw_order_ref not in order_attribution
    assert capture._ORDER_IDENTIFIER_SENTINEL in order_attribution

    trade_updates = json.loads((fixture_root / "trade_updates" / "trade_updates.json").read_text())
    assert [frame["data"].get("event") for frame in trade_updates] == [None, "fill"]
    assert trade_updates[1]["data"]["order"]["client_order_id"] == capture._ORDER_IDENTIFIER_SENTINEL


@pytest.mark.asyncio
async def test_websocket_setup_failure_does_not_hang_or_submit_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSocket:
        async def __aenter__(self) -> None:
            raise OSError("websocket unavailable")

        async def __aexit__(self, *args: object) -> None:
            return None

    class Client:
        submitted = False

        async def submit_order(self, _: dict[str, Any]) -> dict[str, Any]:
            self.submitted = True
            return {}

    monkeypatch.setattr(
        capture,
        "get_alpaca_settings",
        lambda: SimpleNamespace(api_key_id="key", api_secret_key="secret"),
    )
    monkeypatch.setattr(capture.websockets, "connect", lambda *args, **kwargs: FailingSocket())
    client = Client()

    with pytest.raises((OSError, RuntimeError), match="websocket"):
        await asyncio.wait_for(capture._run_order_gate(client), timeout=0.5)

    assert client.submitted is False
