"""Durable broker symbol-validity observations (#1795).

Retire exists to clear a registration that can never admit again, and one of
the two proofs of that class is a symbol the broker does not recognise. That
proof cannot be manufactured on the read path — #1776 bars read endpoints from
invoking the broker port — so this module implements the Two-Tap shape (#1773):
one background tap (the reconciliation sweep's post-pass probe) produces the
fact durably, and the consumers (the panel action guard and the retire commit
boundary) read it passively.

The store is deliberately **not** part of the pinned clerk SQLite contract.
Symbol validity is reconstructible broker evidence, not custody: losing this
file costs one re-probe, so it does not belong in the hash-chained,
lease-fenced custody database. It is a single JSON file under the clerk
artifacts root, written atomically by exactly one writer (the active clerk's
sweep — ``set_active_clerk_runtime`` installs one live authority per process).

Permanence semantics: ``resolvable=False`` is recorded only when the broker
answered definitively — ``get_asset`` maps Alpaca's HTTP 404 to ``None`` ("the
symbol is not a listed asset"), and any other broker failure raises and is
*not* recorded, so an unreachable broker can never mint a false permanence
fact. A symbol that later lists self-heals: unresolvable observations are
re-probed once per ``refresh_ttl_ms``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from app.broker.contract.errors import BrokerError
from app.broker.contract.ports import BrokerReadPort
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_STORE_FILENAME = "symbol_validity.json"


class SymbolValidityObservation(BaseModel):
    """One broker answer about one symbol, timestamped ``int64 ms UTC``."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    observed_at_ms: int
    # False iff the broker definitively answered "this symbol is not a listed
    # asset" (get_asset -> None). True records the asset exists, with its
    # tradability evidence retained for a future, wider predicate.
    resolvable: bool
    tradable: bool = False
    status: str | None = None


class SymbolValidityStore:
    """File-backed observation store: ``<root>/accounts/alpaca/symbol_validity.json``.

    Reads are tolerant — a missing or unparseable file is an empty store
    (surfaced in the log, never raised), because every consumer fails closed
    on absence: no observation means retire stays refused.
    """

    def __init__(self, root: Path) -> None:
        self._path = Path(root) / "accounts" / "alpaca" / _STORE_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def read_all(self) -> dict[str, SymbolValidityObservation]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            payload = json.loads(raw)
            return {
                symbol: SymbolValidityObservation.model_validate(entry)
                for symbol, entry in payload.items()
            }
        except (json.JSONDecodeError, ValidationError, AttributeError):
            logger.warning(
                "symbol-validity store unreadable; treating as empty",
                extra={"action": "symbol_validity_store_unreadable", "path": str(self._path)},
                exc_info=True,
            )
            return {}

    def read(self, symbol: str) -> SymbolValidityObservation | None:
        return self.read_all().get(symbol.upper())

    def record(self, observations: Sequence[SymbolValidityObservation]) -> None:
        """Merge ``observations`` into the store atomically (tmp + replace)."""
        if not observations:
            return
        merged = self.read_all()
        for observation in observations:
            merged[observation.symbol.upper()] = observation
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {symbol: merged[symbol].model_dump() for symbol in sorted(merged)},
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)


class SymbolValidityProbe:
    """Post-pass probe: observe due roster symbols through the guarded read port.

    Due = never observed, or last observed unresolvable more than
    ``refresh_ttl_ms`` ago (so a later-listed symbol self-heals). Resolvable
    observations are terminal for probing purposes, which keeps the
    steady-state broker cost at zero. ``min_interval_ms`` decouples the probe
    cadence from the sweep's 15 s pass so the roster provider is not re-read
    every pass.
    """

    def __init__(
        self,
        *,
        store: SymbolValidityStore,
        read: BrokerReadPort,
        roster_symbols: Callable[[], Sequence[str]],
        now_ms: Callable[[], int] = now_ms_utc,
        min_interval_ms: int = 300_000,
        refresh_ttl_ms: int = 3_600_000,
        max_probes_per_pass: int = 5,
    ) -> None:
        self._store = store
        self._read = read
        self._roster_symbols = roster_symbols
        self._now_ms = now_ms
        self._min_interval_ms = min_interval_ms
        self._refresh_ttl_ms = refresh_ttl_ms
        self._max_probes_per_pass = max_probes_per_pass
        self._last_ran_ms: int | None = None

    async def run_due(self) -> None:
        now_ms = self._now_ms()
        if self._last_ran_ms is not None and now_ms - self._last_ran_ms < self._min_interval_ms:
            return
        self._last_ran_ms = now_ms
        observed = self._store.read_all()
        due = [
            symbol
            for symbol in sorted({symbol.upper() for symbol in self._roster_symbols()})
            if self._is_due(observed.get(symbol), now_ms)
        ][: self._max_probes_per_pass]
        observations: list[SymbolValidityObservation] = []
        for symbol in due:
            try:
                asset = await self._read.get_asset(symbol)
            except BrokerError:
                # An unavailable broker is not evidence about the symbol;
                # record nothing and let a later pass retry.
                logger.warning(
                    "symbol-validity probe could not reach the broker; skipping",
                    extra={"action": "symbol_validity_probe_unavailable", "symbol": symbol},
                    exc_info=True,
                )
                continue
            observations.append(
                SymbolValidityObservation(
                    symbol=symbol,
                    observed_at_ms=now_ms,
                    resolvable=asset is not None,
                    tradable=asset.tradable if asset is not None else False,
                    status=asset.status if asset is not None else None,
                )
            )
        self._store.record(observations)

    def _is_due(self, observation: SymbolValidityObservation | None, now_ms: int) -> bool:
        if observation is None:
            return True
        if observation.resolvable:
            return False
        return now_ms - observation.observed_at_ms >= self._refresh_ttl_ms


def _store_root() -> Path:
    """The one production store root (the clerk artifacts dir).

    Kept as a seam on purpose: ``get_alpaca_settings`` validates credentials
    on first use, and tests must never depend on them — the autouse
    ``_isolate_symbol_validity_store`` fixture in ``tests/conftest.py``
    repoints this at an empty per-test location (the #1739 lesson: a
    gitignored artifacts file must not make local pytest diverge from CI).
    """
    from app.broker.alpaca.config import get_alpaca_settings

    return get_alpaca_settings().clerk_dir


def symbol_marked_unresolvable(symbol: str) -> bool:
    """True iff the durable store holds a definitive "symbol unlisted" answer.

    The canonical read-side predicate for #1795's retire widening: pure file
    read, no broker I/O, fail-closed — no observation means False, so retire
    stays refused until the sweep has actually asked the broker.
    """
    observation = SymbolValidityStore(_store_root()).read(symbol)
    return observation is not None and not observation.resolvable
