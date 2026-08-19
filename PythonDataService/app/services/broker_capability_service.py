from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from app.broker.ibkr.capability import probe_session_data_capability
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.config import get_settings
from app.schemas.broker_capability import SessionDataCapability
from app.services.session_authority import session_state_at_ms

logger = logging.getLogger(__name__)


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
            snapshot = self._read_snapshot(path)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def read_latest_for(self, symbol: str, account_id: str) -> SessionDataCapability | None:
        """Return the newest snapshot scoped to one instrument and account."""
        directory = self._safe_snapshot_dir(account_id, symbol)
        if not directory.exists():
            return None
        timestamped = sorted(path for path in directory.glob("*.json") if path.name != "latest.json")
        candidates = timestamped or [directory / "latest.json"]
        matching = [
            snapshot
            for path in candidates
            if (snapshot := self._read_snapshot(path)) is not None
            and snapshot.symbol == symbol.upper()
            and snapshot.account_id == account_id
        ]
        return max(matching, key=lambda snapshot: snapshot.probed_at_ms, default=None)

    def persist(self, snapshot: SessionDataCapability) -> None:
        directory = self._safe_snapshot_dir(snapshot.account_id, snapshot.symbol)
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True)
        latest = directory / "latest.json"
        timestamped = directory / f"{snapshot.probed_at_ms}.json"
        timestamped.write_text(payload + "\n", encoding="utf-8")
        current_latest = self._read_snapshot(latest)
        if current_latest is None or current_latest.probed_at_ms <= snapshot.probed_at_ms:
            latest.write_text(payload + "\n", encoding="utf-8")

    def _read_snapshot(self, path: Path) -> SessionDataCapability | None:
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as fh:
                return SessionDataCapability.model_validate_json(fh.read())
        except (OSError, ValidationError, json.JSONDecodeError) as exc:
            logger.warning(
                "Ignoring unreadable broker capability snapshot",
                extra={
                    "action": "broker_capability_snapshot_ignored",
                    "path": str(path),
                    "error": str(exc),
                },
            )
            return None

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


def extended_phase_proven_at_ms(*, now_ms: int, symbol: str, account_id: str) -> bool:
    """Whether extended-session (PRE/POST/OVERNIGHT) capability is proven
    for this instrument/account right now.

    Resolves the capability snapshot this service owns, then defers the
    actual session decision to the pure, injection-only
    ``session_authority.session_state_at_ms`` — that module has no service
    dependencies of its own, so the I/O lookup lives here instead. Shared
    by every caller that must reconcile a broker's RTH-only live clock
    (which reports CLOSED outside regular hours regardless of actual
    extended-session availability) against the canonical session
    authority: the ENTER gate in ``bot_trade_strategy.py`` and its
    Clerk-boundary recheck in ``runtime.py`` (#1671), via
    ``market_liveness.liveness_blocks_entry``.
    """
    capability = get_broker_capability_service().read_latest_for(symbol=symbol, account_id=account_id)
    return session_state_at_ms(
        now_ms=now_ms,
        capability=capability,
        symbol=symbol,
        account_id=account_id,
    ).extended_phase_proven
