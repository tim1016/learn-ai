"""Live-runtime run identity — ``LiveRunLedger`` and builder.

Per spec ``docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md``
section 10. Distinct from the backtest-side ``app.research.runs.ledger.RunLedger``
which captures deterministic-replay identity for a backtest; this ledger
captures the inputs that pin a *live* paper run's identity.

``run_id`` = ``sha256(canonical_json(identity_payload))`` over:
  * ``code_sha`` — git HEAD on run-start commit. Required, must be non-empty.
    The dirty-tree refusal in ``pre_flight.check_clean_tree`` is what makes
    this meaningful — runs from a dirty tree do not start.
  * ``strategy_spec_path`` + ``strategy_spec_sha256`` — the
    ``StrategySpec`` JSON contract being run.
  * ``qc_audit_copy_sha256`` — sha256 of the checked-in QC audit copy
    (``references/qc-shadow/SpyEmaCrossoverAlgorithm.py``).
  * ``qc_cloud_backtest_id`` — operator-supplied identifier of the QC
    Cloud backtest that proves the QC Cloud execution copy is in sync
    with the audit copy.
  * ``live_config`` — resolved values, not raw env vars.
  * ``account_id`` — DU… account id from IBKR.
  * ``start_date_ms`` — int64 ms UTC, the first bar's session start.

Reuses the canonical-JSON SHA-256 helper at
``app.research.runs.hashing`` rather than reinventing the contract.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.live_state_sidecar import _fsync_parent_dir
from app.research.runs.hashing import canonical_json, hash_payload

HydratePolicy = Literal["require", "optional", "disabled"]


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_ms_utc() -> int:
    return int(time.time() * 1000)


class LiveRunStartDefaults(BaseModel):
    """Non-hashed operator start defaults captured at deploy time."""

    model_config = ConfigDict(extra="forbid")

    strategy: str = ""
    readonly: bool = True
    hydrate_policy: HydratePolicy = "require"
    max_orders_per_day: int = Field(default=2_000, ge=0, le=100_000)
    ibkr_host: str = "127.0.0.1"


class LiveRunLedger(BaseModel):
    """Immutable identity record for a single live paper run.

    Persisted as ``run_ledger.json`` under
    ``live_runs/<run_id>/run_ledger.json``. Once written it is treated
    as read-only — a halted run keeps its ledger; a resumed run gets a
    new ``run_id`` (§ 7.2 #5).
    """

    model_config = ConfigDict(extra="forbid")

    # 1.1 adds ``strategy_instance_id`` (UI-0 identity binding). 1.2 adds
    # ``strategy_key`` (#416 — the hand-coded algorithm module the run starts
    # under). 1.3 adds ADR 0009's engine-derived sizing stamps
    # (``governed_by`` + ``sizing_provenance``). 1.4 adds non-hashed operator
    # start defaults captured at deploy time. NONE of the added fields are
    # part of the ``run_id`` hash, so existing 1.0–1.2 run_ids, run directories,
    # and fixtures stay byte-identical. A legacy ledger that predates a field
    # has no key for it; the defaults below let it read cleanly as
    # "unknown / legacy".
    schema_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4"] = "1.4"

    run_id: str
    code_sha: str

    # Stable identifier for the configured strategy instance (UI-0). Keyed
    # by the durable desired-state sidecar at
    # ``artifacts/live_state/<strategy_instance_id>/``. Persisted here so a
    # fresh, pre-decision run has an O(1) ``run_id -> strategy_instance_id``
    # mapping. Deliberately NOT hashed into ``run_id`` (see ``compute_run_id``).
    # Empty string = legacy / unknown (a 1.0 ledger read without the field).
    strategy_instance_id: str = ""

    # The hand-coded algorithm module this run is meant to start under (the
    # ``--strategy`` arg to ``run start``; #416). Recorded at init-ledger so
    # the console can default the Start card from it AND ``run start`` can
    # reject a ``--strategy`` inconsistent with it — closing the foot-gun where
    # a mismatched algorithm silently runs against a ledger reconciled to a
    # different QC backtest. Deliberately NOT hashed into ``run_id``. Empty
    # string = legacy / unknown; the guard and the default both no-op when empty.
    strategy_key: str = ""

    strategy_spec_path: str
    strategy_spec_sha256: str

    qc_audit_copy_path: str
    qc_audit_copy_sha256: str
    qc_cloud_backtest_id: str

    account_id: str
    start_date_ms: int

    # Resolved live config (not raw env vars). Kept as a dict so the
    # ledger remains stable across LiveConfig field additions.
    live_config: dict

    # ADR 0009 § 3 — two engine-derived sizing stamps. Neither is hashed into
    # ``run_id`` (the policy choice IS hashed via ``live_config.sizing``; these
    # stamps are derivative facts about *who/what authorized* the resolved
    # sizing). The operator never types these; ``build_ledger`` derives them.
    # ``governed_by`` ∈ {live_config, strategy_explicit} — who set the quantity.
    # ``sizing_provenance`` ∈ {reference_native, live_override, spec_default} —
    # does the resolved live sizing equal the bound QC audit copy's sizing rule?
    # PR1 always emits ``live_override`` (the fail-closed default); PR3 wires
    # ``reference_native`` via the audit-copy allow-list. ``spec_default`` is
    # reserved (ADR § 3) and not emitted today. Empty strings here are NOT a
    # legal in-band value — a 1.0/1.1/1.2 ledger that predates the fields lacks
    # the keys entirely and ``model_validate`` falls back to the defaults,
    # which lets old fixtures read cleanly.
    governed_by: Literal["live_config", "strategy_explicit"] = "live_config"
    sizing_provenance: Literal["reference_native", "live_override", "spec_default"] = (
        "live_override"
    )

    start_defaults: LiveRunStartDefaults | None = None

    created_at_ms: int = Field(default_factory=_now_ms_utc)


def compute_run_id(
    *,
    code_sha: str,
    strategy_spec_sha256: str,
    qc_audit_copy_sha256: str,
    qc_cloud_backtest_id: str,
    account_id: str,
    start_date_ms: int,
    live_config: dict,
) -> str:
    """Hash the run-identity payload to produce a stable ``run_id``.

    Excludes ``created_at_ms`` so re-running the same identity inputs
    yields the same id (the ledger persists the timestamp separately,
    but the hash is over the identity, not the wall-clock).
    """
    payload = {
        "code_sha": code_sha,
        "strategy_spec_sha256": strategy_spec_sha256,
        "qc_audit_copy_sha256": qc_audit_copy_sha256,
        "qc_cloud_backtest_id": qc_cloud_backtest_id,
        "account_id": account_id,
        "start_date_ms": start_date_ms,
        "live_config": live_config,
    }
    return hash_payload(payload)


def build_ledger(
    *,
    code_sha: str,
    strategy_spec_path: Path,
    qc_audit_copy_path: Path,
    qc_cloud_backtest_id: str,
    account_id: str,
    start_date_ms: int,
    live_config: dict,
    strategy_instance_id: str = "",
    strategy_key: str = "",
    start_defaults: LiveRunStartDefaults | None = None,
    audit_copy_allow_list_root: Path | None = None,
) -> LiveRunLedger:
    """Build a ``LiveRunLedger`` from on-disk inputs and resolved config.

    Reads the spec JSON and the QC audit copy file, hashes them, and
    constructs the identity. Raises ``FileNotFoundError`` if either
    referenced path is missing — fail fast at run-start before any
    broker connection.

    ``strategy_instance_id`` (UI-0) and ``strategy_key`` (#416) are both
    persisted but deliberately left out of ``compute_run_id`` — adding them
    must not change ``run_id`` for existing runs. They default to ``""``
    (legacy / unknown) so existing callers that don't supply them produce an
    identical ``run_id``.
    """
    if not strategy_spec_path.exists():
        raise FileNotFoundError(f"strategy_spec_path does not exist: {strategy_spec_path}")
    if not qc_audit_copy_path.exists():
        raise FileNotFoundError(f"qc_audit_copy_path does not exist: {qc_audit_copy_path}")

    strategy_spec_sha256 = _file_sha256(strategy_spec_path)
    qc_audit_copy_sha256 = _file_sha256(qc_audit_copy_path)
    run_id = compute_run_id(
        code_sha=code_sha,
        strategy_spec_sha256=strategy_spec_sha256,
        qc_audit_copy_sha256=qc_audit_copy_sha256,
        qc_cloud_backtest_id=qc_cloud_backtest_id,
        account_id=account_id,
        start_date_ms=start_date_ms,
        live_config=live_config,
    )
    # ADR 0009 § 3 — engine-derive the two sizing stamps from the resolved
    # ``live_config.sizing`` (when present). Absence ⇒ legacy/unknown, which
    # carries the conservative defaults on the ledger model (governed_by =
    # live_config since the legacy ``SimpleFloorSizing`` was de facto the
    # ``set_holdings`` path; sizing_provenance = live_override since there is
    # no proof path for it).
    #
    # PR3 wires ``reference_native`` via the audit-copy allow-list: when the
    # resolved policy is rule-equivalent to the audit copy's registered rule
    # AND the audit copy's sha re-verifies, the stamp goes to
    # ``reference_native``. Every other outcome (mismatch, sha drift, file
    # missing, allow-list missing) falls to the fail-closed ``live_override``.
    from app.engine.execution.audit_copy_allow_list import lookup as _audit_copy_lookup
    from app.engine.execution.order_sizer import (
        governed_by,
        parse_sizing_policy,
    )

    # Validate ``sizing`` by **key presence**, not truthiness (PR1 reviewer
    # fix). A falsy payload (``{}`` / ``None`` / ``""``) past the API boundary
    # is a deploy bug — writing it would persist an unstartable ledger because
    # the start gate parses on key presence and would reject it. Hand it to
    # ``parse_sizing_policy`` so the deploy fails fast with the same error
    # surface the start gate uses. Genuine absence (no key at all) keeps
    # legacy/unknown semantics on the ledger stamps.
    sizing_present = isinstance(live_config, dict) and "sizing" in live_config
    if sizing_present:
        resolved_policy = parse_sizing_policy(live_config["sizing"])
    else:
        resolved_policy = None

    sizing_provenance: Literal["reference_native", "live_override", "spec_default"] = (
        "live_override"
    )
    if resolved_policy is not None:
        # The allow-list stores repo-relative POSIX paths (the canonical form
        # ADR 0006 uses everywhere); compute that form from the absolute path
        # the daemon resolved before handing off here.
        lookup_path = str(qc_audit_copy_path)
        if audit_copy_allow_list_root is not None:
            try:
                lookup_path = (
                    Path(qc_audit_copy_path)
                    .resolve()
                    .relative_to(Path(audit_copy_allow_list_root).resolve())
                    .as_posix()
                )
            except ValueError:
                # The audit copy is outside the repo root — the lookup will
                # legitimately surface as cannot_prove.
                lookup_path = str(qc_audit_copy_path)
        verdict = _audit_copy_lookup(
            audit_copy_path=lookup_path,
            proposed_policy=resolved_policy,
            repo_root=audit_copy_allow_list_root,
        )
        if verdict.verdict == "proven_match":
            sizing_provenance = "reference_native"
    return LiveRunLedger(
        run_id=run_id,
        code_sha=code_sha,
        strategy_spec_path=str(strategy_spec_path),
        strategy_spec_sha256=strategy_spec_sha256,
        qc_audit_copy_path=str(qc_audit_copy_path),
        qc_audit_copy_sha256=qc_audit_copy_sha256,
        qc_cloud_backtest_id=qc_cloud_backtest_id,
        account_id=account_id,
        start_date_ms=start_date_ms,
        live_config=live_config,
        strategy_instance_id=strategy_instance_id,
        strategy_key=strategy_key,
        start_defaults=start_defaults,
        governed_by=governed_by(resolved_policy),
        sizing_provenance=sizing_provenance,
    )


def write_ledger(path: Path, ledger: LiveRunLedger) -> None:
    """Write the ledger as canonical JSON for stable on-disk hashing.

    Uses the same canonical-JSON formatter as ``compute_run_id`` so the
    on-disk bytes are identical across runs with identical inputs — this
    means the SHA-256 of ``run_ledger.json`` (which appears in the daily
    Markdown's hash manifest, § 6.5) is deterministic. The file is
    published through a sibling temp file, fsync, atomic replace, and
    parent-directory fsync so a crash cannot leave a torn ledger.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = ledger.model_dump(mode="json")
    _atomic_write_text(path, canonical_json(payload))


def read_ledger(path: Path) -> LiveRunLedger:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return LiveRunLedger.model_validate(payload)


def _atomic_write_text(path: Path, payload: str) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=False,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_parent_dir(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
