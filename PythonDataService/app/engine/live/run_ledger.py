"""Temporary read-only parser for legacy ``run_ledger.json`` evidence.

Per spec ``docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md``
section 10. Distinct from the backtest-side ``app.research.runs.ledger.RunLedger``
which captures deterministic-replay identity for a backtest; this ledger
captures the inputs that pinned a retired IBKR paper run's identity.

Historical ``run_id`` values were content-addressed over:
  * ``code_sha`` — git HEAD on run-start commit. Required, must be non-empty.
    The retired deploy plane refused dirty trees before creating a run.
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

Creation and mutation retired with the IBKR evaluator control plane in #1636.
This parser remains only for the ADR-0037 step-5 legacy-custody retirement and
must not be imported by a bot-control mutation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

HydratePolicy = Literal["require", "optional", "disabled"]


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


class RedeployLineage(BaseModel):
    """Redeploy provenance — the parent run this run was deployed to replace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_run_id: str
    redeploy_reason: str | None = None
    redeployed_at_ms: int


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
    # start defaults captured at deploy time. 1.5 adds ``lineage`` (redeploy
    # parent_run_id and reason). NONE of the added fields are part of the
    # ``run_id`` hash, so existing 1.0–1.4 run_ids, run directories, and
    # fixtures stay byte-identical. A legacy ledger that predates a field has
    # no key for it; the defaults below let it read cleanly as "unknown / legacy".
    schema_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4", "1.5"] = "1.5"

    run_id: str
    code_sha: str

    # Stable identifier for the configured strategy instance. Empty string
    # means legacy / unknown (a 1.0 ledger read without the field).
    strategy_instance_id: str = ""

    # The hand-coded algorithm module used by the historical run. Empty string
    # means legacy / unknown.
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

    # ADR 0009 § 3 — two historical engine-derived sizing stamps. Neither was
    # hashed into ``run_id``; the policy choice was captured in
    # ``live_config.sizing``.
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
    lineage: RedeployLineage | None = None

    created_at_ms: int = Field(default_factory=_now_ms_utc)


def read_ledger(path: Path) -> LiveRunLedger:
    """Parse an existing legacy ledger without creating or modifying it."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return LiveRunLedger.model_validate(payload)
