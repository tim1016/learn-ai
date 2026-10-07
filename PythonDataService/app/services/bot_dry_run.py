"""Durable simulated activity for zero-broker-write bot runs."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.services.jsonl_wal import JsonlWal
from app.utils.session_anchors import MAX_TIMESTAMP_MS

DRY_RUN_ACTIVITY_FILENAME = "dry_run_activity.jsonl"
MAX_DRY_RUN_TAIL = 500


# What only a fill in an option contract carries. Each is left out of a row that has none.
_OPTION_FILL_FIELDS = ("underlying", "multiplier", "fill_model", "quote_age_ms", "contract_reference")


class DryRunActivity(BaseModel):
    """One simulated fill with its selected synthetic custody authority.

    A fill in shares: ``symbol`` is the stock, ``quantity`` shares and
    ``fill_price`` dollars a share. A fill in an option contract (ADR 0076):
    ``symbol`` is the contract's OCC symbol, as on the Clerk's own fill rows,
    ``quantity`` is contracts, ``fill_price`` is the premium, dollars a share,
    and the fill's cash is contracts x premium x ``multiplier``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    strategy_instance_id: str
    run_id: str
    authority_account_id: str
    authority_kind: Literal["synthetic"]
    recorded_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    bar_ref: str
    intent: Literal["ENTER", "EXIT"]
    order_ref: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0)
    fill_price: float = Field(gt=0)
    # Set only when the fill was priced from a bar that closed before it was
    # decided: that bar's close, so the price's age is on the row (#2912).
    price_bar_end_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    # Set only for a fill in an option contract (ADR 0076). The symbol the contract is on.
    underlying: str | None = None
    # The shares one contract's premium stands for, as the Clerk's books record the contract.
    multiplier: int | None = Field(default=None, ge=1)
    # How the simulation priced the fill, and how old the quote side it used was then.
    fill_model: str | None = None
    quote_age_ms: int | None = Field(default=None, ge=0)
    # On an entry: where the contract's listing was read. A simulation has no broker reference to ask.
    contract_reference: Literal["ibkr_listing"] | None = None
    simulated: Literal[True] = True

    @model_serializer(mode="wrap")
    def _omit_what_is_absent(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """A fill in shares, priced at its own minute, writes the row it always wrote.

        The journal refuses a field it does not know, so a row carrying a
        null one could not be read by the code before the field existed.
        """
        row = handler(self)
        for name in ("price_bar_end_ms", *_OPTION_FILL_FIELDS):
            if row.get(name) is None:
                row.pop(name, None)
        return row

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_authority(cls, value: Any) -> Any:
        """Lift pre-authority rows without weakening current evidence checks."""
        if not isinstance(value, dict):
            return value
        lifted = dict(value)
        if "authority_account_id" not in lifted:
            strategy_instance_id = lifted.get("strategy_instance_id")
            if isinstance(strategy_instance_id, str):
                lifted["authority_account_id"] = synthetic_account_id_for_strategy(
                    strategy_instance_id
                )
        lifted.setdefault("authority_kind", "synthetic")
        return lifted

    @model_validator(mode="after")
    def _require_authority_for_instance(self) -> DryRunActivity:
        expected = synthetic_account_id_for_strategy(self.strategy_instance_id)
        if self.authority_account_id != expected:
            raise ValueError("Dry Run activity authority does not match strategy instance")
        return self

    @model_validator(mode="after")
    def _an_option_fill_says_what_it_multiplies_by(self) -> DryRunActivity:
        """A row is a fill in shares or a fill in a contract, never half of each: a premium with no
        multiplier would be read as a share price."""
        if (self.underlying is None) != (self.multiplier is None):
            raise ValueError("an option fill names both its underlying and its contract's multiplier")
        of_an_option_only = (self.fill_model, self.quote_age_ms, self.contract_reference)
        if self.multiplier is None and any(value is not None for value in of_an_option_only):
            raise ValueError("only a fill in an option contract says how its quote priced it")
        return self


def _corrupt_error(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Dry-run activity journal corrupt at {path}: {detail}")


class DryRunActivityJournal:
    """Fsync'd append-only WAL kept beside one immutable strategy instance."""

    def __init__(self, instance_dir: Path) -> None:
        resolved = instance_dir.resolve()
        self._wal: JsonlWal[DryRunActivity] = JsonlWal(
            resolved / DRY_RUN_ACTIVITY_FILENAME,
            record_model=DryRunActivity,
            corrupt_error=_corrupt_error,
            seq_of=lambda row: row.seq,
            label="dry_run_activity",
            trusted_root=resolved,
        )
        self._lock = threading.Lock()

    def append(self, activity: DryRunActivity) -> None:
        with self._lock:
            self._wal.append(activity)

    def next_seq(self) -> int:
        with self._lock:
            return self._wal.allocate_seq()

    def tail(self, limit: int) -> list[DryRunActivity]:
        if limit <= 0:
            raise ValueError("dry-run activity tail limit must be positive")
        return self._wal.read_tail(limit=min(limit, MAX_DRY_RUN_TAIL))
