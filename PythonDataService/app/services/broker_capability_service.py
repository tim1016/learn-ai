from __future__ import annotations

import json
from pathlib import Path

from app.broker.ibkr.capability import probe_session_data_capability
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.config import get_settings
from app.schemas.broker_capability import SessionDataCapability


class BrokerCapabilityService:
    def __init__(self, *, root: Path | None = None) -> None:
        settings = get_settings()
        self._root = root or Path(settings.live_runs_root) / "_broker" / "session_capabilities"

    async def probe(
        self,
        client: IbkrClient,
        *,
        symbols: list[str],
    ) -> list[SessionDataCapability]:
        snapshots: list[SessionDataCapability] = []
        for symbol in symbols:
            snapshot = await probe_session_data_capability(client, symbol=symbol)
            self.persist(snapshot)
            snapshots.append(snapshot)
        return snapshots

    def read_latest(self) -> list[SessionDataCapability]:
        snapshots: list[SessionDataCapability] = []
        if not self._root.exists():
            return snapshots
        for path in sorted(self._root.glob("*/*/latest.json")):
            with path.open(encoding="utf-8") as fh:
                snapshots.append(SessionDataCapability.model_validate_json(fh.read()))
        return snapshots

    def persist(self, snapshot: SessionDataCapability) -> None:
        directory = self._safe_snapshot_dir(snapshot.account_id, snapshot.symbol)
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True)
        latest = directory / "latest.json"
        timestamped = directory / f"{snapshot.probed_at_ms}.json"
        timestamped.write_text(payload + "\n", encoding="utf-8")
        latest.write_text(payload + "\n", encoding="utf-8")

    def _safe_snapshot_dir(self, account_id: str, symbol: str) -> Path:
        root = self._root.resolve()
        account_part = _sanitize_path_part(account_id)
        symbol_part = _sanitize_path_part(symbol.upper())
        path = (root / account_part / symbol_part).resolve()
        root_prefix = f"{root}{Path('/')}"
        if str(path) != str(root) and not str(path).startswith(root_prefix):
            raise ValueError("capability snapshot path escaped root")
        return path


def _sanitize_path_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)
    return cleaned.strip("._") or "unknown"


_SERVICE = BrokerCapabilityService()


def get_broker_capability_service() -> BrokerCapabilityService:
    return _SERVICE
