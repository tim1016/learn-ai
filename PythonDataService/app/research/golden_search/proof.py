"""Golden Search proof: the exact inputs a qualification replayed, and the replay itself.

Formula: a proof binds one Signal Program configuration — program key and
version, stock, canonical parameters — over one window ``[start_ms, end_ms)``
primed from ``warmup_from_ms`` to the decision trace it produces. Every
receipted lake artifact that read touches (each session's minute zip from the
warmup session through the window's last session, its corporate-action
companion, and for adjusted data the corporate-action basis the companions
name) is copied into a content-addressed blob store at ``root/<sha[:2]>/<sha>``
— written through an fsynced temporary and an atomic rename, then re-read and
verified. The program is replayed twice through
``BacktestEngine.for_decision_identity`` with every read bound to that
manifest: once over the live lake, once over a tree restored from the blobs
alone. The proof holds only when both replays yield the same ``trace_root``
and trace count. Any byte that does not match its receipt is a mismatch,
never a skipped file. A re-proof replays the restored tree under the code now
running and must reproduce the recorded root.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Proof
  creation" and "Runtime proof"; ``app/engine/strategy/signal_program.py``
  for ``trace_root``.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_proof.py.

What a proof is, and is not: repeatability evidence for these recorded inputs
under the loaded executable identity. Identical traces here say nothing about
any other input, and nothing about agreement with LEAN.

The functions below read the lake through ``read_committed_bytes``, which must
not run on an event loop: call them from a worker thread.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from app.config import settings
from app.data_lake.adjustment_versions import (
    AdjustmentVersionError,
    adjusted_root_for,
    companion_path,
    current_snapshot_path,
)
from app.data_lake.admission import LakeAdmissionError, read_committed_bytes
from app.data_lake.path_policy import LeanMinuteBarPath, lake_root_for
from app.engine.engine import BacktestEngine, pin_strategy_window
from app.engine.live.identity import confine_path_to_root
from app.engine.strategy.params import StrategyParamsBase
from app.engine.strategy.registry import _STRATEGY_REGISTRY, SignalProgramContract, StrategyRegistration
from app.engine.strategy.signal_program import trace_root
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.sweep.snapshot import DataSnapshot, DataSnapshotMismatchError, ManifestBoundMinuteReader
from app.schemas.signal_program_seal import semantic_payload_hash
from app.utils.atomic_file import atomic_write_bytes
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

PROOF_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
# The restored tree is laid out like the lake root it replaces, so an
# adjusted replay verifies its companions and basis exactly as the lake read
# did. Its parent is never named ``lake``: ``lake_root_for`` would then treat
# it as a managed root and demand catalog admission for bytes the blob store
# already verified.
_ADJUSTED_ROOT_NAME = "polygon_split_adjusted"
_RAW_ROOT_NAME = "raw"

ProofMismatchCode = Literal[
    "INPUT_NOT_RECEIPTED",
    "INPUT_MISSING",
    "RECEIPT_MISMATCH",
    "BLOB_MISSING",
    "BLOB_CORRUPT",
    "EMPTY_REPLAY",
    "TRACE_MISMATCH",
    "PROGRAM_CHANGED",
    "PARAMETERS_CHANGED",
]


class ProofMismatchError(RuntimeError):
    """The proof does not hold: traces differ, or a byte failed its receipt.

    Never overridable by a research acknowledgement (#2696): a technical
    proof failure publishes no qualification.
    """

    def __init__(self, message: str, *, code: ProofMismatchCode) -> None:
        super().__init__(message)
        self.code = code


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class BlobStore:
    """Content-addressed, verified-on-read storage for proof inputs."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, digest: str) -> Path:
        if not _SHA256.fullmatch(digest):
            raise ValueError(f"not a sha256 blob address: {digest!r}")
        return confine_path_to_root(self._root / digest[:2] / digest, self._root, label="proof blob")

    def put(self, payload: bytes) -> str:
        """Store ``payload`` durably and return its sha256 address.

        Idempotent: a blob already holding these bytes is left alone. One
        that exists but no longer hashes to its address is rewritten from
        the bytes in hand, and the rewrite is logged. Either way the stored
        file is re-read and verified before the address is returned.
        """
        digest = _sha256(payload)
        path = self._path(digest)
        existing = path.read_bytes() if path.is_file() else None
        if existing is None or _sha256(existing) != digest:
            if existing is not None:
                logger.warning(
                    "Rewriting a corrupt Golden Search proof blob from verified bytes",
                    extra={"action": "golden_proof_blob_rewritten", "blob_sha256": digest},
                )
            atomic_write_bytes(path, payload)
        stored = path.read_bytes()
        if _sha256(stored) != digest:
            raise ProofMismatchError(f"proof blob {digest[:12]}… did not read back as written", code="BLOB_CORRUPT")
        return digest

    def get(self, digest: str) -> bytes:
        """The bytes stored at ``digest``, refused unless they still hash to it."""
        path = self._path(digest)
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise ProofMismatchError(f"proof blob {digest[:12]}… is missing", code="BLOB_MISSING") from exc
        actual = _sha256(payload)
        if actual != digest:
            raise ProofMismatchError(
                f"proof blob {digest[:12]}… is corrupt (its bytes hash to {actual[:12]}…)", code="BLOB_CORRUPT"
            )
        return payload


def default_blob_store() -> BlobStore:
    """The deployment's durable proof store (``GOLDEN_SEARCH_PROOF_ROOT``)."""
    return BlobStore(settings.GOLDEN_SEARCH_PROOF_ROOT)


def _require_et_midnight(name: str, value: int) -> None:
    if et_midnight_ms(et_date_at_ms(value)) != value:
        raise ValueError(f"{name} must be an ET-midnight anchor, got {value}")


@dataclass(frozen=True)
class ProofWindow:
    """The scored window ``[start_ms, end_ms)`` and the session its warmup reads from.

    All three are ET-midnight ``int64 ms UTC`` anchors. ``warmup_from_ms``
    equal to ``start_ms`` means an unprimed replay; earlier, the engine
    primes on the warmup sessions and crosses into evaluation at
    ``start_ms`` exactly as a primed study evaluation does.
    """

    start_ms: int
    end_ms: int
    warmup_from_ms: int

    def __post_init__(self) -> None:
        for name in ("start_ms", "end_ms", "warmup_from_ms"):
            _require_et_midnight(name, getattr(self, name))
        if not self.warmup_from_ms <= self.start_ms < self.end_ms:
            raise ValueError("a proof window needs warmup_from_ms <= start_ms < end_ms")

    @property
    def read_start(self) -> date:
        return et_date_at_ms(self.warmup_from_ms)

    @property
    def read_end(self) -> date:
        return et_date_at_ms(self.end_ms - 1)

    @property
    def evaluation_start_ms(self) -> int | None:
        return self.start_ms if self.warmup_from_ms < self.start_ms else None


@dataclass(frozen=True)
class ProofInputs:
    """The staged manifest: every artifact a proof replay may read, by its receipted digest."""

    symbol: str
    adjusted: bool
    # Root-relative POSIX path -> sha256, sorted by path.
    artifacts: dict[str, str]


def _stage(blob_store: BlobStore, relative: str, payload: bytes, receipted: str | None) -> str:
    if receipted is None:
        raise ProofMismatchError(f"{relative} is not part of the study's data receipt", code="INPUT_NOT_RECEIPTED")
    actual = _sha256(payload)
    if actual != receipted:
        raise ProofMismatchError(
            f"{relative} changed since the study's data receipt (receipted {receipted[:12]}…, found {actual[:12]}…)",
            code="RECEIPT_MISMATCH",
        )
    return blob_store.put(payload)


def _read_input(path: Path, relative: str) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise ProofMismatchError(f"{relative} is missing from the lake", code="INPUT_MISSING") from exc


def stage_proof_inputs(
    snapshot: DataSnapshot,
    *,
    roots: Sequence[Path],
    window: ProofWindow,
    blob_store: BlobStore,
) -> ProofInputs:
    """Copy the receipted artifacts the proof window reads into ``blob_store``.

    Only the sessions from ``window.read_start`` through ``window.read_end``
    are staged, not the whole study snapshot. Each zip is read through lake
    admission, then every staged byte must hash to its receipt: a session the
    receipt never covered, a file now missing, or bytes that moved are all
    refusals.
    """
    if snapshot.resolution != "minute":
        raise ValueError(f"a proof replays minute data, not a {snapshot.resolution} snapshot")
    sessions = expected_sessions(window.read_start, window.read_end)
    if not sessions:
        raise ValueError("the proof window holds no trading session")
    staged: dict[str, str] = {}
    adjusted_roots: set[Path] = set()
    raw_sessions = 0
    for day in sessions:
        minute = LeanMinuteBarPath(market="usa", symbol=snapshot.symbol, trading_date=day, data_type="trade")
        relative = minute.relative_path()
        path = next((Path(root) / relative for root in roots if (Path(root) / relative).is_file()), None)
        if path is None:
            raise ProofMismatchError(f"{relative} is missing from the lake", code="INPUT_MISSING")
        try:
            payload = read_committed_bytes(path)
        except LakeAdmissionError as exc:
            raise ProofMismatchError(f"{relative} has no committed lake receipt", code="RECEIPT_MISMATCH") from exc
        staged[str(relative)] = _stage(blob_store, str(relative), payload, snapshot.artifacts.get(str(relative)))
        adjusted_root = adjusted_root_for(path)
        if adjusted_root is None:
            raw_sessions += 1
            continue
        adjusted_roots.add(adjusted_root)
        companion = str(companion_path(relative))
        staged[companion] = _stage(
            blob_store, companion, _read_input(companion_path(path), companion), snapshot.artifacts.get(companion)
        )
    if adjusted_roots and (raw_sessions or len(adjusted_roots) > 1):
        raise ValueError("a proof replays one data root; these sessions resolve to more than one")
    if adjusted_roots:
        (adjusted_root,) = adjusted_roots
        basis = str(current_snapshot_path(snapshot.symbol))
        staged[basis] = _stage(
            blob_store,
            basis,
            _read_input(adjusted_root / basis, basis),
            snapshot.corporate_action_versions.get(snapshot.symbol.upper()),
        )
    return ProofInputs(symbol=snapshot.symbol, adjusted=bool(adjusted_roots), artifacts=dict(sorted(staged.items())))


def materialize(inputs: ProofInputs, blob_store: BlobStore, dest_root: Path) -> Path:
    """Write every staged blob at its root-relative path under ``dest_root``; return the data root.

    Each blob is verified on read. The returned root is named like the lake
    root it stands in for, so the replay's adjustment checks run unchanged.
    """
    data_root = Path(dest_root) / (_ADJUSTED_ROOT_NAME if inputs.adjusted else _RAW_ROOT_NAME)
    if lake_root_for(data_root / "probe") is not None:
        raise ValueError(f"a restored proof tree must not sit inside a managed lake root: {data_root}")
    data_root.mkdir(parents=True, exist_ok=True)
    for relative, digest in inputs.artifacts.items():
        target = confine_path_to_root(data_root / relative, data_root, label="restored proof input")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob_store.get(digest))
    return data_root


@dataclass(frozen=True)
class TraceProof:
    trace_root: str
    trace_count: int


def _signal_program(strategy_key: str) -> tuple[StrategyRegistration, SignalProgramContract]:
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    contract = None if registration is None else registration.signal_program_contract
    if registration is None or registration.signal_program_factory is None or contract is None:
        raise ValueError(f"{strategy_key!r} is not a registered, qualifiable Signal Program")
    return registration, contract


def _validated_params(registration: StrategyRegistration, symbol: str, params: Mapping[str, Any]) -> StrategyParamsBase:
    """The program's parameters exactly as the registry schema accepts them, ``symbol`` included."""
    supplied = params.get("symbol")
    if supplied is not None and str(supplied).upper() != symbol.upper():
        raise ValueError(f"parameters name symbol {supplied!r}, not {symbol!r}")
    return registration.param_schema.model_validate({**params, "symbol": symbol})


def _canonical_params(registration: StrategyRegistration, symbol: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The one canonical point: the schema's own JSON dump, ``symbol`` included."""
    return _validated_params(registration, symbol, params).model_dump(mode="json")


def decision_trace(
    strategy_key: str,
    symbol: str,
    params: Mapping[str, Any],
    window: ProofWindow,
    *,
    roots: Sequence[Path],
    manifest: Mapping[str, str],
) -> TraceProof:
    """Replay the registered program over ``window`` and return its decision-trace root.

    The program is built from the registry exactly as Deploy builds it, its
    run window is pinned through the engine's own ``pin_strategy_window``
    seam, and every bar comes through a reader bound to ``manifest``. The
    root covers every committed decision from the first warmup bar on; the
    engine's evaluation boundary asserts the program is ready at
    ``start_ms``, so a warmup too short fails here rather than tracing a
    cold start.
    """
    registration, _contract = _signal_program(strategy_key)
    strategy = registration.build(_validated_params(registration, symbol, params))
    program = getattr(strategy, "signal_program", None)
    if program is None:
        raise ValueError(f"{strategy_key!r} built a strategy with no active signal program")
    pin_strategy_window(strategy, window.read_start, window.read_end)
    reader = ManifestBoundMinuteReader(list(roots), manifest, session="regular")
    try:
        BacktestEngine.for_decision_identity(reader).run(
            strategy, evaluation_start_ms=window.evaluation_start_ms, retain_bars=False
        )
    except (DataSnapshotMismatchError, AdjustmentVersionError, LakeAdmissionError) as exc:
        raise ProofMismatchError(f"a replay read failed its receipt: {exc}", code="RECEIPT_MISMATCH") from exc
    traces = program.session.traces
    if not any(trace.bar_close_ms >= window.start_ms for trace in traces):
        raise ProofMismatchError("the replay made no decision inside the proof window", code="EMPTY_REPLAY")
    return TraceProof(trace_root=trace_root(traces), trace_count=len(traces))


@dataclass(frozen=True)
class ProofRecord:
    """The immutable proof a qualification stores (``proof_json``); its sha256 is ``proof_sha256``."""

    program_key: str
    program_version: str
    symbol: str
    params: dict[str, Any]
    window: ProofWindow
    adjusted: bool
    manifest: dict[str, str]
    lake_trace_root: str
    restored_trace_root: str
    trace_count: int
    artifact_digest: str
    wiring_digest: str
    created_at_ms: int
    schema_version: int = PROOF_SCHEMA_VERSION

    @property
    def inputs(self) -> ProofInputs:
        return ProofInputs(symbol=self.symbol, adjusted=self.adjusted, artifacts=dict(self.manifest))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "program_key": self.program_key,
            "program_version": self.program_version,
            "symbol": self.symbol,
            "params": dict(self.params),
            "window": {"start_ms": self.window.start_ms, "end_ms": self.window.end_ms},
            "warmup_from_ms": self.window.warmup_from_ms,
            "adjusted": self.adjusted,
            "manifest": dict(sorted(self.manifest.items())),
            "lake_trace_root": self.lake_trace_root,
            "restored_trace_root": self.restored_trace_root,
            "trace_count": self.trace_count,
            "artifact_digest": self.artifact_digest,
            "wiring_digest": self.wiring_digest,
            "created_at_ms": self.created_at_ms,
        }

    def sha256(self) -> str:
        """``proof_sha256``: sha256 of the record's canonical JSON."""
        return semantic_payload_hash(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProofRecord:
        try:
            if payload["schema_version"] != PROOF_SCHEMA_VERSION:
                raise ValueError(f"unsupported proof schema version {payload['schema_version']!r}")
            manifest = {str(path): str(digest) for path, digest in dict(payload["manifest"]).items()}
            return cls(
                program_key=str(payload["program_key"]),
                program_version=str(payload["program_version"]),
                symbol=str(payload["symbol"]),
                params=dict(payload["params"]),
                window=ProofWindow(
                    start_ms=int(payload["window"]["start_ms"]),
                    end_ms=int(payload["window"]["end_ms"]),
                    warmup_from_ms=int(payload["warmup_from_ms"]),
                ),
                adjusted=_strict_bool(payload["adjusted"], "adjusted"),
                manifest=manifest,
                lake_trace_root=str(payload["lake_trace_root"]),
                restored_trace_root=str(payload["restored_trace_root"]),
                trace_count=int(payload["trace_count"]),
                artifact_digest=str(payload["artifact_digest"]),
                wiring_digest=str(payload["wiring_digest"]),
                created_at_ms=int(payload["created_at_ms"]),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"malformed proof record: {exc}") from exc


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"malformed proof record: {name} must be a boolean")
    return value


def _restored_trace(
    strategy_key: str,
    symbol: str,
    params: Mapping[str, Any],
    window: ProofWindow,
    inputs: ProofInputs,
    blob_store: BlobStore,
    scratch_dir: Path | None,
) -> TraceProof:
    with tempfile.TemporaryDirectory(prefix="golden-proof-", dir=scratch_dir) as scratch:
        data_root = materialize(inputs, blob_store, Path(scratch))
        return decision_trace(strategy_key, symbol, params, window, roots=[data_root], manifest=inputs.artifacts)


def build_proof(
    *,
    strategy_key: str,
    symbol: str,
    params: Mapping[str, Any],
    window: ProofWindow,
    snapshot: DataSnapshot,
    roots: Sequence[Path],
    blob_store: BlobStore,
    artifact_digest: str,
    wiring_digest: str,
    created_at_ms: int | None = None,
    scratch_dir: Path | None = None,
) -> ProofRecord:
    """Stage the window's inputs, replay them over the lake and from the blobs, and require one root.

    ``artifact_digest`` / ``wiring_digest`` name the executable identity the
    replays ran under; the caller supplies the running process's digests
    after confirming the code on disk is the code it imported.
    """
    registration, contract = _signal_program(strategy_key)
    if snapshot.symbol != symbol.upper():
        raise ValueError(f"the data snapshot is for {snapshot.symbol}, not {symbol.upper()}")
    canonical = _canonical_params(registration, snapshot.symbol, params)
    inputs = stage_proof_inputs(snapshot, roots=roots, window=window, blob_store=blob_store)
    lake = decision_trace(strategy_key, snapshot.symbol, canonical, window, roots=roots, manifest=inputs.artifacts)
    restored = _restored_trace(strategy_key, snapshot.symbol, canonical, window, inputs, blob_store, scratch_dir)
    if restored != lake:
        raise ProofMismatchError(
            f"the restored replay produced {restored.trace_count} decisions with root {restored.trace_root[:12]}…, "
            f"the lake replay {lake.trace_count} with root {lake.trace_root[:12]}…",
            code="TRACE_MISMATCH",
        )
    return ProofRecord(
        program_key=strategy_key,
        program_version=contract.program_version,
        symbol=snapshot.symbol,
        params=canonical,
        window=window,
        adjusted=inputs.adjusted,
        manifest=inputs.artifacts,
        lake_trace_root=lake.trace_root,
        restored_trace_root=restored.trace_root,
        trace_count=restored.trace_count,
        artifact_digest=artifact_digest,
        wiring_digest=wiring_digest,
        created_at_ms=now_ms_utc() if created_at_ms is None else created_at_ms,
    )


def reprove(
    record: ProofRecord,
    blob_store: BlobStore,
    *,
    artifact_digest: str,
    wiring_digest: str,
    created_at_ms: int | None = None,
    scratch_dir: Path | None = None,
) -> ProofRecord:
    """Replay a recorded proof's restored inputs under the code running now.

    Applies only to the same program version and the same canonical
    parameters: a changed program or parameter schema needs a new study, not
    a re-proof. The returned record carries the new digests and the
    re-derived restored root; ``lake_trace_root`` stays the original lake
    replay's, because a re-proof never reads the lake.
    """
    registration, contract = _signal_program(record.program_key)
    running_version = contract.program_version
    if running_version != record.program_version:
        raise ProofMismatchError(
            f"the program moved from {record.program_version} to {running_version}; a changed program "
            "needs a new Golden Search study, not a re-proof",
            code="PROGRAM_CHANGED",
        )
    try:
        canonical = _canonical_params(registration, record.symbol, record.params)
    except ValidationError as exc:
        raise ProofMismatchError(
            f"the recorded parameters no longer validate: {exc.error_count()} error(s)", code="PARAMETERS_CHANGED"
        ) from exc
    if canonical != record.params:
        raise ProofMismatchError(
            "the parameter schema now writes these parameters differently; a re-proof cannot carry them",
            code="PARAMETERS_CHANGED",
        )
    restored = _restored_trace(
        record.program_key, record.symbol, canonical, record.window, record.inputs, blob_store, scratch_dir
    )
    if restored != TraceProof(trace_root=record.restored_trace_root, trace_count=record.trace_count):
        raise ProofMismatchError(
            f"the running code replays {restored.trace_count} decisions with root {restored.trace_root[:12]}…; "
            f"the proof recorded {record.trace_count} with root {record.restored_trace_root[:12]}…",
            code="TRACE_MISMATCH",
        )
    return dataclasses.replace(
        record,
        restored_trace_root=restored.trace_root,
        artifact_digest=artifact_digest,
        wiring_digest=wiring_digest,
        created_at_ms=now_ms_utc() if created_at_ms is None else created_at_ms,
    )


__all__ = [
    "PROOF_SCHEMA_VERSION",
    "BlobStore",
    "ProofInputs",
    "ProofMismatchError",
    "ProofRecord",
    "ProofWindow",
    "TraceProof",
    "build_proof",
    "decision_trace",
    "default_blob_store",
    "materialize",
    "reprove",
    "stage_proof_inputs",
]
