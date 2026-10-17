"""Repeatability evidence for an exact Options Backtest setup.

The selected run's receipted input bytes are archived in the existing content
addressed proof store. The current replay must reproduce the selected run,
then reproduce it again from those blobs without reading the lake. This is
repeatability evidence, never an independent validation or a profitability claim.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.data_lake.admission import read_committed_bytes
from app.data_lake.option_bars import parse_option_day_zip
from app.data_lake.option_lake import option_read_root
from app.data_lake.option_listing import OptionListing, parse_option_listing_zip
from app.data_lake.path_policy import LeanMinuteBarPath, OptionListingPath, OptionMinuteBarPath
from app.engine.data.lean_format import LeanMinuteDataReader
from app.lean_sidecar.trading_calendar import CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION
from app.options.contract import OptionContract
from app.options_backtest.data import Captured, RunData
from app.options_backtest.lake_data import (
    LakeRunData,
    UnderlyingDayUnreadable,
    _named_by_close,
    _require_every_row_decodable,
    captured_listing_of,
)
from app.options_backtest.market import MinuteBar
from app.options_backtest.picking import Listing
from app.options_backtest.run import RunResult, RunStopped, run_sessions
from app.options_backtest.setup import OptionsBacktestSetup
from app.research.golden_search.proof import BlobStore, default_blob_store
from app.schemas.signal_program_seal import semantic_payload_hash
from app.utils.session_anchors import et_date_at_ms

_APP_ROOT = Path(__file__).resolve().parents[1]


class GoldenProofError(ValueError):
    """Input identities or replay outcomes do not reproduce the reviewed run."""


def _canonical(value: Any) -> Any:
    """Exact JSON identity; Decimal spelling changes in NUMERIC storage are neutral."""
    if isinstance(value, Decimal):
        text = format(value, "f")
        return (text.rstrip("0").rstrip(".") if "." in text else text) if value else "0"
    if isinstance(value, OptionContract):
        return value.occ_symbol
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="python"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical(getattr(value, field.name))
            for field in dataclasses.fields(value)
            # Money steps are not stored with historical runs. Their derived
            # capital evidence is compared through the summary instead.
            if field.name not in {"money_steps", "stamps"}
        }
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, date):
        raise TypeError("proof identities carry instants, never date strings")
    return value


def setup_sha256(setup: OptionsBacktestSetup) -> str:
    return semantic_payload_hash(setup.model_dump(mode="json"))


def result_sha256(result: RunResult) -> str:
    return semantic_payload_hash(_canonical(result))


def _source_digest() -> str:
    paths = {
        *(_APP_ROOT / "options").glob("*.py"),
        *(
            _APP_ROOT / "options_backtest" / name
            for name in (
                "setup.py", "replay.py", "run.py", "picking.py", "fills.py", "cover_book.py", "fees.py",
                "session_clock.py", "session_market.py", "session_levels.py", "session_series.py", "results.py",
                "classification.py", "needs.py", "statistics.py", "capital_evidence.py", "cash_ladder.py",
                "run_stamps.py", "lake_data.py",
                "sessions.py", "limits.py", "market.py", "session_result.py", "observation.py",
            )
        ),
        *(_APP_ROOT / "options_bots").glob("session*.py"),
        _APP_ROOT / "options_bots/execution.py",
        _APP_ROOT / "broker/alpaca/option_fees.py",
        _APP_ROOT / "broker/alpaca/regulatory_fees.py",
        _APP_ROOT / "broker/alpaca/clerk/money.py",
        _APP_ROOT / "lean_sidecar/trading_calendar.py",
        _APP_ROOT / "utils/session_anchors.py",
        _APP_ROOT / "data_lake/option_bars.py",
        _APP_ROOT / "data_lake/option_listing.py",
        _APP_ROOT / "engine/data/lean_format.py",
    }
    return semantic_payload_hash(
        {
            "sources": {
                str(path.relative_to(_APP_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(paths)
            },
            "calendar": CALENDAR_IDENTITY,
            "calendar_package": CALENDAR_PACKAGE_VERSION,
        }
    )


_LOADED_RULES_DIGEST = _source_digest()


def current_rules_digest() -> str:
    """Identify the running rules only while their source tree still agrees."""
    try:
        current = _source_digest()
    except OSError as exc:
        raise GoldenProofError("The running Options rules cannot be read. Qualification is unavailable.") from exc
    if current != _LOADED_RULES_DIGEST:
        raise GoldenProofError("Options rules changed on disk. Restart this process before qualifying a setup.")
    return _LOADED_RULES_DIGEST


def input_manifest(result: RunResult) -> dict[str, str]:
    """Paths and recorded byte identities, derived solely from the selected run."""
    manifest: dict[str, str] = {}
    for session in result.sessions:
        day = et_date_at_ms(session.session_open_ms)
        pins = session.pins
        if pins.underlying_sha256 is not None:
            manifest[str(LeanMinuteBarPath("usa", result.setup.underlying, day, "trade").relative_path())] = (
                pins.underlying_sha256
            )
        if pins.listing_sha256 is not None:
            manifest[str(OptionListingPath(result.setup.underlying, day).relative_path())] = pins.listing_sha256
        for pinned in pins.option_days:
            path = OptionMinuteBarPath(pinned.contract.root, pinned.contract.occ_symbol, day).relative_path()
            manifest[str(path)] = pinned.sha256
    return manifest


class ArchivedRunData:
    """RunData decoding only the archived, hash-verified original file bytes."""

    def __init__(self, underlying: str, manifest: Mapping[str, str], blobs: BlobStore) -> None:
        self.underlying = underlying
        self.manifest = dict(manifest)
        self.blobs = blobs

    def _read(self, relative: str) -> tuple[bytes, str]:
        if relative not in self.manifest:
            raise GoldenProofError("The replay requested an input outside the approved evidence.")
        digest = self.manifest[relative]
        return self.blobs.get(digest), digest

    def underlying_day(self, trading_date: date) -> Captured[Sequence[MinuteBar]]:
        relative = str(LeanMinuteBarPath("usa", self.underlying, trading_date, "trade").relative_path())
        payload, digest = self._read(relative)
        _require_every_row_decodable(payload, self.underlying, trading_date)
        reader = LeanMinuteDataReader(Path("/options-proof/raw"), session="regular")
        bars = reader.parse_day_zip(payload, self.underlying, trading_date)
        return Captured(
            _named_by_close(bars, trading_date, subject="archived underlying", unreadable=UnderlyingDayUnreadable),
            digest,
        )

    def listing(self, trading_date: date) -> Captured[Listing]:
        payload, digest = self._read(str(OptionListingPath(self.underlying, trading_date).relative_path()))
        return captured_listing_of(
            OptionListing(
                self.underlying,
                trading_date,
                parse_option_listing_zip(payload, self.underlying, trading_date),
                digest,
            )
        )

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[Sequence[MinuteBar]]:
        relative = str(OptionMinuteBarPath(contract.root, contract.occ_symbol, trading_date).relative_path())
        payload, digest = self._read(relative)
        bars = parse_option_day_zip(payload, contract.occ_symbol, trading_date)
        return Captured(
            _named_by_close(bars, trading_date, subject="archived contract", unreadable=UnderlyingDayUnreadable), digest
        )


def _require_not_stopped(should_stop: Callable[[], bool] | None, sessions_done: int = 0) -> None:
    if should_stop is not None and should_stop():
        raise RunStopped(sessions_done)


def _replay(
    source: RunResult, data: RunData, *, should_stop: Callable[[], bool] | None = None,
    on_session: Callable[[int, int, date], None] | None = None,
) -> RunResult:
    callbacks: dict[str, Any] = {}
    if should_stop is not None:
        callbacks["should_stop"] = should_stop
    if on_session is not None:
        callbacks["on_session"] = on_session
    return run_sessions(
        source.setup,
        tuple(et_date_at_ms(session.session_open_ms) for session in source.sessions),
        data,
        slippage_ladder=tuple(point.slippage_percent for point in source.slippage),
        cash_ladder=tuple(level.multiple for level in (source.summary.cash_ladder or ())),
        **callbacks,
    )


def build_proof(
    source: RunResult, *, blobs: BlobStore | None = None, lake_root: Path | None = None,
    source_data: RunData | None = None, should_stop: Callable[[], bool] | None = None,
    on_session: Callable[[int, int, date], None] | None = None,
) -> dict[str, Any]:
    """Reproduce the complete run twice, using retained inputs when a frozen reader is supplied.

    The manual path archives and verifies the selected lake bytes as before.
    The retained-study path requires those exact blobs already to exist and
    never consults the lake. Both independently decode a second replay from
    the source run's pins, and compare complete outcomes and input identities.
    """
    _require_not_stopped(should_stop)
    rules_digest = current_rules_digest()
    if not source.sessions or source.summary.stamps is None:
        raise GoldenProofError("This run has no recorded rules or sessions. Run the setup again before approval.")
    if not source.summary.cash_ladder:
        raise GoldenProofError("This run has no recorded capital evidence. Run the setup again before approval.")
    store = blobs or default_blob_store()
    manifest = input_manifest(source)
    if not manifest:
        raise GoldenProofError("This run read no market inputs and cannot qualify a trading setup.")
    if source_data is None:
        root = option_read_root(lake_root)
        for relative, expected in manifest.items():
            _require_not_stopped(should_stop)
            payload = read_committed_bytes(root / relative)
            if hashlib.sha256(payload).hexdigest() != expected:
                raise GoldenProofError("An input changed since the selected run. Nothing was approved.")
            if store.put(payload) != expected:
                raise GoldenProofError("An input could not be archived under its recorded identity.")
        source_data = LakeRunData(source.setup.underlying, lake_root=lake_root)
    else:
        for expected in manifest.values():
            _require_not_stopped(should_stop)
            # BlobStore verifies every digest on read and refuses missing or corrupt bytes.
            store.get(expected)
    expected_outcome = result_sha256(source)
    replayed = _replay(source, source_data, should_stop=should_stop, on_session=on_session)
    if result_sha256(replayed) != expected_outcome:
        raise GoldenProofError("The current replay does not reproduce the selected run's recorded outcomes.")
    restored = _replay(source, ArchivedRunData(source.setup.underlying, manifest, store),
                       should_stop=should_stop, on_session=on_session)
    if result_sha256(restored) != expected_outcome:
        raise GoldenProofError("Replaying archived inputs does not reproduce the selected run's recorded outcomes.")
    if current_rules_digest() != rules_digest:
        raise GoldenProofError("The Options rules changed while the proof was running.")
    _require_not_stopped(should_stop, len(source.sessions))
    return {
        "schema_version": 1,
        "rules_digest": rules_digest,
        "setup_sha256": setup_sha256(source.setup),
        "result_sha256": expected_outcome,
        "manifest": manifest,
        "calendar_identity": CALENDAR_IDENTITY,
        "calendar_package_version": CALENDAR_PACKAGE_VERSION,
        "session_opens_ms": [session.session_open_ms for session in source.sessions],
    }


def reprove(source: RunResult, proof: Mapping[str, Any], *, blobs: BlobStore | None = None) -> dict[str, Any]:
    """Reprove from archived bytes alone, without consulting a mutable lake."""
    rules_digest = current_rules_digest()
    if setup_sha256(source.setup) != proof["setup_sha256"] or result_sha256(source) != proof["result_sha256"]:
        raise GoldenProofError("The retained source evidence no longer matches the approved proof.")
    manifest = proof["manifest"]
    restored = _replay(source, ArchivedRunData(source.setup.underlying, manifest, blobs or default_blob_store()))
    if result_sha256(restored) != proof["result_sha256"]:
        raise GoldenProofError("The current Options rules do not reproduce the approved outcomes.")
    if current_rules_digest() != rules_digest:
        raise GoldenProofError("The Options rules changed while the proof was running.")
    return {**proof, "rules_digest": rules_digest}


def proof_sha256(proof: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(proof), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
