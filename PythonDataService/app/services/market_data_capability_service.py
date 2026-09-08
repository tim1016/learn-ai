from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.ibkr.capability import probe_session_data_capability
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.config import get_settings
from app.schemas.broker_capability import SessionDataCapability
from app.services.session_authority import EXTENDED_PHASES, session_state_at_ms

logger = logging.getLogger(__name__)


class MarketDataCapabilityService:
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


_SERVICE = MarketDataCapabilityService()


def get_market_data_capability_service() -> MarketDataCapabilityService:
    return _SERVICE


def extended_phase_proven_at_ms(
    *,
    now_ms: int,
    symbol: str,
    account_id: str | None,
    extended_window: ExtendedHoursWindow | None = None,
) -> bool:
    """Whether extended-session (PRE/POST/OVERNIGHT) capability is proven
    for this instrument/account right now.

    ``account_id`` is ``str | None`` because every caller resolves it from
    something that can legitimately come up empty (a market-data feed with
    no capability identity, a panel rendered before an account is known) —
    each one would otherwise have to repeat the same "no account, no proof"
    guard before calling in. ``None`` fails closed here **only for the
    capability path**: a declared ``extended_window`` is not account-scoped
    (ADR 0059 D5.2), so when one is supplied it still resolves the session
    with no capability lookup and no account.

    Resolves the capability snapshot this service owns (when an account is
    given), then defers the actual session decision to the pure,
    injection-only ``session_authority.session_state_at_ms`` — that module
    has no service dependencies of its own, so the I/O lookup lives here
    instead. Shared by every caller that must reconcile a broker's RTH-only
    live clock (which reports CLOSED outside regular hours regardless of
    actual extended-session availability) against the canonical session
    authority: the ENTER gate in ``bot_trade_strategy.py`` and its
    Clerk-boundary recheck in ``runtime.py`` (#1671), via
    ``market_liveness.liveness_blocks_entry``.

    ``session.extended_phase_proven`` is a *provenance* flag — it only says
    the answer came from a fresh, matching capability snapshot or a
    declared window, rather than the bare NYSE calendar. It is also true
    when that snapshot resolves to ``RTH`` or ``CLOSED``, so checking it
    alone would let a running ``use_rth=False`` bot override fresh,
    correct Alpaca ``CLOSED`` evidence during e.g. an emergency RTH
    closure. Only a *resolved phase* of PRE, POST, or OVERNIGHT counts as
    "proven extended" — not ``session.permits_strategy_activity``, which
    answers a different question (whether the *caller's own*
    allowed-sessions policy, defaulted here to RTH-only since none is
    supplied, permits the resolved phase) and would be false for every
    extended phase by construction.

    **This answers the schedule, never liveness.** Both sources it resolves
    — a declared broker window and a capability snapshot — describe the
    session that was *supposed* to run at ``now_ms``; neither can see an
    unscheduled PRE/POST closure, which Alpaca's RTH-only clock reports as
    plain ``CLOSED``, exactly as it reports an ordinary extended session.
    So a ``True`` here is a necessary condition for admitting extended
    exposure and never a sufficient one: ``market_liveness.
    liveness_blocks_entry`` pairs it with ``market_data_bars_live`` — the
    feed actually printing bars for the symbol — and admits only on both
    (ADR 0022: the calendar owns scheduled structure, the live feed owns
    liveness).
    """
    if account_id is None and extended_window is None:
        return False
    capability: SessionDataCapability | None = None
    if account_id is not None:
        capability = get_market_data_capability_service().read_latest_for(symbol=symbol, account_id=account_id)
    session = session_state_at_ms(
        now_ms=now_ms,
        capability=capability,
        symbol=symbol,
        account_id=account_id,
        extended_window=extended_window,
    )
    return session.extended_phase_proven and session.phase in EXTENDED_PHASES
