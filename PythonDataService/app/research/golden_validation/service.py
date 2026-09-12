"""Deep module for the Validation Golden Run workflow.

The module freezes a history run as an exact validation case, authors the
current computed evidence view, and appends human review decisions.  A human
may accept agreement, reviewed deviations, or unavailable/corrupt evidence;
the original computed state is always preserved and is never rewritten as
agreement.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from app.research.backtest_runs import repository as backtest_repo
from app.research.golden_validation import repository as repo
from app.utils.timestamps import now_ms_utc

Decision = Literal["accept", "reject"]
EvidenceState = Literal[
    "agreement",
    "deviations",
    "pending",
    "unavailable",
    "run_failed",
    "persist_failed",
    "missing",
    "corrupt",
]
_CERTIFICATE_PARITY_VERDICT_VERSION = 3
_RECEIPT_STATUSES = frozenset({"match", "mismatch"})


class GoldenValidationError(ValueError):
    """Base class for an authored workflow refusal."""


class GoldenRunNotFoundError(GoldenValidationError):
    pass


class GoldenRunIneligibleError(GoldenValidationError):
    pass


class CommandConflictError(GoldenValidationError):
    pass


class StaleEvidenceError(GoldenValidationError):
    def __init__(self, current_revision: str) -> None:
        super().__init__("The parity evidence changed after this review form was opened.")
        self.current_revision = current_revision


@dataclass(frozen=True, slots=True)
class EvidenceView:
    state: EvidenceState
    revision: str
    parity_verdict_id: int | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GoldenValidationDossier:
    golden_run: repo.GoldenRunRow
    validation_case: dict[str, Any]
    evidence: EvidenceView
    reviews: tuple[repo.GoldenReviewRow, ...]

    @property
    def latest_review(self) -> repo.GoldenReviewRow | None:
        return self.reviews[0] if self.reviews else None

    @property
    def review_is_current(self) -> bool | None:
        review = self.latest_review
        return None if review is None else review.expected_evidence_revision == self.evidence.revision

    @property
    def state(self) -> str:
        review = self.latest_review
        if review is None:
            return "candidate"
        if review.decision == "reject":
            return "rejected"
        return f"accepted_{review.classification}"


@dataclass(frozen=True, slots=True)
class GoldenDeploymentCandidates:
    """Exact deployment candidates plus the strategy's migration state."""

    dossiers: tuple[GoldenValidationDossier, ...]
    strategy_has_golden_runs: bool


@dataclass(frozen=True, slots=True)
class ApplicabilityReceipt:
    golden_validation_id: int
    applicable: bool
    state: str
    classification: str | None
    mismatched_fields: tuple[str, ...]
    explanation: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _parse_object(raw: str | None, *, field: str, allow_none: bool = False) -> dict[str, Any] | None:
    if raw is None and allow_none:
        return None
    try:
        parsed = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise GoldenRunIneligibleError(f"The source run has unreadable {field} evidence.") from exc
    if not isinstance(parsed, dict):
        raise GoldenRunIneligibleError(f"The source run's {field} evidence is not an object.")
    return parsed


def _case_snapshot(run: backtest_repo.RunDetail) -> dict[str, Any]:
    parameters = _parse_object(run.parameters_json, field="parameters")
    data_policy = _parse_object(run.data_policy_json, field="data policy", allow_none=True)
    execution_configuration = _parse_object(
        run.execution_config_json,
        field="execution configuration",
        allow_none=True,
    )
    return {
        "schema_version": 1,
        "source_run_id": run.id,
        "strategy": {
            "name": run.strategy_name,
            # Version 7 records this at run persistence time. Historical rows
            # remain null and therefore require a visible manual override;
            # current registry data never backfills historical evidence.
            "program_version": run.program_version,
        },
        "symbol": run.symbol,
        "parameters": parameters,
        "window": {
            "start_ms": run.start_ms,
            "end_ms": run.end_ms,
            "timespan": run.timespan,
        },
        "data_policy": data_policy,
        "execution": {
            "fill_mode": run.fill_mode,
            "initial_cash": run.initial_cash,
            "commission_per_order": run.commission_per_order,
            "brokerage_policy": run.brokerage_policy,
            "configuration": execution_configuration,
        },
        "requested_engine": run.requested_engine,
        "parity_group_id": run.parity_group_id,
    }


def _command_hash(payload: dict[str, Any]) -> str:
    return _sha256(payload)


def _is_certificate_grade_v3(
    golden_run: repo.GoldenRunRow,
    verdict: Mapping[str, Any],
    payload: Mapping[str, Any],
    status: str,
) -> bool:
    """Validate the closed v3 evidence shape before granting a parity classification."""
    validation_case = json.loads(golden_run.validation_case_json)
    strategy = validation_case.get("strategy")
    program_version = strategy.get("program_version") if isinstance(strategy, dict) else None
    left_run_id = verdict["left_run_id"]
    right_run_id = verdict["right_run_id"]
    if (
        verdict["verdict_version"] != _CERTIFICATE_PARITY_VERDICT_VERSION
        or payload.get("schema_version") != _CERTIFICATE_PARITY_VERDICT_VERSION
        or payload.get("status") != status
        or payload.get("parity_group_id") != verdict["parity_group_id"]
        or payload.get("left_execution_id") != left_run_id
        or payload.get("right_execution_id") != right_run_id
        or left_run_id != golden_run.source_run_id
        or right_run_id is None
        or payload.get("engines") != {"left": "python", "right": "lean"}
    ):
        return False

    tolerances = payload.get("tolerances")
    fill_price_atol = tolerances.get("fill_price_atol") if isinstance(tolerances, Mapping) else None
    if not isinstance(fill_price_atol, str) or not fill_price_atol.strip():
        return False

    receipts: dict[str, Mapping[str, Any]] = {}
    for name in (
        "native_metric_parity",
        "readiness_parity",
        "input_parity",
        "parameter_parity",
        "program_version_parity",
    ):
        receipt = payload.get(name)
        if not isinstance(receipt, Mapping) or receipt.get("status") not in _RECEIPT_STATUSES:
            return False
        receipts[name] = receipt

    if receipts["input_parity"].get("status") != "match" or receipts["parameter_parity"].get("status") != "match":
        return False
    native_receipt = receipts["native_metric_parity"]
    native_divergence_count = native_receipt.get("divergence_count")
    if (
        not isinstance(native_receipt.get("contract_id"), str)
        or not native_receipt["contract_id"].strip()
        or not isinstance(native_receipt.get("source_commit"), str)
        or not native_receipt["source_commit"].strip()
        or not isinstance(native_receipt.get("absolute_tolerance"), int | float)
        or isinstance(native_receipt.get("absolute_tolerance"), bool)
        or native_receipt["absolute_tolerance"] < 0
        or not isinstance(native_receipt.get("native_metric_count"), int)
        or isinstance(native_receipt.get("native_metric_count"), bool)
        or native_receipt["native_metric_count"] <= 0
        or not isinstance(native_receipt.get("formatted_metric_count"), int)
        or isinstance(native_receipt.get("formatted_metric_count"), bool)
        or native_receipt["formatted_metric_count"] <= 0
        or not isinstance(native_divergence_count, int)
        or isinstance(native_divergence_count, bool)
        or native_divergence_count < 0
        or (native_receipt.get("status") == "match" and native_divergence_count != 0)
        or (native_receipt.get("status") == "mismatch" and native_divergence_count == 0)
    ):
        return False
    for name in ("readiness_parity", "input_parity", "parameter_parity"):
        receipt = receipts[name]
        field_count = receipt.get("compared_field_count")
        mismatched_fields = receipt.get("mismatched_fields")
        if (
            not isinstance(field_count, int)
            or isinstance(field_count, bool)
            or field_count <= 0
            or not isinstance(mismatched_fields, list)
            or (receipt.get("status") == "match" and mismatched_fields)
            or (receipt.get("status") == "mismatch" and not mismatched_fields)
        ):
            return False
    version_receipt = receipts["program_version_parity"]
    if (
        version_receipt.get("status") != "match"
        or version_receipt.get("left_program_version") != program_version
        or version_receipt.get("right_program_version") != program_version
    ):
        return False
    fixture_id = receipts["input_parity"].get("fixture_id")
    fixture_sha256 = receipts["input_parity"].get("fixture_sha256")
    if (
        not isinstance(fixture_id, str)
        or not fixture_id.strip()
        or not isinstance(fixture_sha256, str)
        or len(fixture_sha256) != 64
        or any(character not in "0123456789abcdef" for character in fixture_sha256.lower())
    ):
        return False

    divergences = payload.get("divergences")
    counts = payload.get("counts_by_category")
    computed_at_ms = payload.get("computed_at_ms")
    if (
        not isinstance(divergences, list)
        or not isinstance(counts, Mapping)
        or not isinstance(computed_at_ms, int)
        or isinstance(computed_at_ms, bool)
        or computed_at_ms <= 0
    ):
        return False

    expected_counts: dict[str, int] = {}
    for item in divergences:
        if not isinstance(item, Mapping):
            return False
        category = item.get("category")
        if not isinstance(category, str) or not category or not isinstance(item.get("message"), str):
            return False
        expected_counts[category] = expected_counts.get(category, 0) + 1
    if dict(counts) != expected_counts:
        return False

    has_result_difference = bool(divergences) or any(
        receipts[name].get("status") == "mismatch"
        for name in ("native_metric_parity", "readiness_parity")
    )
    return (status == "agree" and not has_result_difference) or (status == "diverged" and has_result_difference)


def _evidence_for(golden_run: repo.GoldenRunRow, verdict: Mapping[str, Any] | None) -> EvidenceView:
    validation_case = json.loads(golden_run.validation_case_json)
    strategy = validation_case.get("strategy")
    program_version = strategy.get("program_version") if isinstance(strategy, dict) else None
    qualification_warnings: list[str] = []
    if not isinstance(program_version, str) or not program_version:
        qualification_warnings.append("program_version_missing")
    execution = validation_case.get("execution")
    if not isinstance(execution, dict) or not isinstance(execution.get("configuration"), dict):
        qualification_warnings.append("execution_configuration_missing")
    if verdict is None:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "computed_state": "missing",
            "parity_verdict": None,
            "qualification_warnings": qualification_warnings,
        }
        state: EvidenceState = "missing"
        verdict_id = None
    else:
        status = str(verdict["status"])
        state_by_status: dict[str, EvidenceState] = {
            "agree": "agreement",
            "diverged": "deviations",
            "pending": "pending",
            "unavailable": "unavailable",
            "run_failed": "run_failed",
            "persist_failed": "persist_failed",
        }
        try:
            verdict_payload = json.loads(verdict["verdict_json"])
        except (TypeError, json.JSONDecodeError):
            verdict_payload = None
        structurally_valid = (
            isinstance(verdict_payload, dict)
            and status in state_by_status
            and verdict_payload.get("status") in (None, status)
        )
        certificate_grade = (
            structurally_valid
            and _is_certificate_grade_v3(golden_run, verdict, verdict_payload, status)
        )
        if status in {"agree", "diverged"} and not certificate_grade:
            qualification_warnings.append("parity_verdict_not_certificate_grade")
        state = state_by_status.get(status, "corrupt") if structurally_valid else "corrupt"
        if qualification_warnings and state in {"agreement", "deviations"}:
            state = "corrupt"
        verdict_id = int(verdict["id"])
        payload = {
            "schema_version": 1,
            "computed_state": state,
            "qualification_warnings": qualification_warnings,
            "parity_verdict": {
                "id": verdict_id,
                "parity_group_id": verdict["parity_group_id"],
                "left_run_id": verdict["left_run_id"],
                "right_run_id": verdict["right_run_id"],
                "verdict_version": verdict["verdict_version"],
                "status": status,
                "created_at_ms": verdict["created_at_ms"],
                "payload": verdict_payload,
            },
        }
    revision = _sha256({"case_sha256": golden_run.case_sha256, "evidence": payload})
    return EvidenceView(state=state, revision=revision, parity_verdict_id=verdict_id, payload=payload)


async def _load_dossier(
    conn: asyncpg.Connection,
    golden_run: repo.GoldenRunRow,
    *,
    lock_evidence: bool = False,
) -> GoldenValidationDossier:
    validation_case = json.loads(golden_run.validation_case_json)
    parity_group_id = validation_case.get("parity_group_id")
    verdict = (
        await repo.parity_verdict_for_case(conn, parity_group_id, lock=lock_evidence)
        if isinstance(parity_group_id, str) and parity_group_id
        else None
    )
    evidence = _evidence_for(golden_run, verdict)
    reviews = tuple(await repo.list_reviews(conn, golden_run.id))
    return GoldenValidationDossier(golden_run, validation_case, evidence, reviews)


async def designate(
    conn: asyncpg.Connection,
    *,
    source_run_id: int,
    command_id: str,
    label: str | None,
    rationale: str,
    actor: str,
) -> GoldenValidationDossier:
    """Freeze one completed Python history run as a Validation Golden Run."""
    command = {
        "action": "designate",
        "source_run_id": source_run_id,
        "label": label,
        "rationale": rationale,
    }
    command_sha256 = _command_hash(command)
    async with conn.transaction():
        prior_command = await repo.get_golden_run_by_command(conn, command_id)
        if prior_command is not None:
            if prior_command.command_sha256 != command_sha256:
                raise CommandConflictError("That command id was already used for a different designation.")
            return await _load_dossier(conn, prior_command)

        prior_source = await repo.get_golden_run_by_source(conn, source_run_id)
        if prior_source is not None:
            return await _load_dossier(conn, prior_source)

        source = await repo.lock_source_run(conn, source_run_id)
        if source is None:
            raise GoldenRunNotFoundError(f"Backtest run {source_run_id} was not found.")
        if source != "engine":
            raise GoldenRunIneligibleError("Choose the Python side of the run pair as the golden baseline.")
        run = await backtest_repo.get_run(conn, source_run_id, trade_limit=None)
        if run is None:
            raise GoldenRunNotFoundError(f"Backtest run {source_run_id} was not found.")
        validation_case = _case_snapshot(run)
        parity_group_id = validation_case.get("parity_group_id")
        if isinstance(parity_group_id, str) and parity_group_id:
            try:
                await repo.lock_paired_evidence_for_golden_case(conn, parity_group_id)
            except repo.PairedEvidenceLockUnavailableError as exc:
                raise GoldenRunIneligibleError(
                    "The paired engine evidence is changing. Retry the designation after the history update completes."
                ) from exc
        case_json = _canonical_json(validation_case)
        golden_run = await repo.insert_golden_run(
            conn,
            source_run_id=source_run_id,
            command_id=command_id,
            command_sha256=command_sha256,
            label=label,
            strategy_name=run.strategy_name,
            symbol=run.symbol,
            validation_case_json=case_json,
            case_sha256=_sha256(validation_case),
            rationale=rationale,
            designated_by=actor,
            designated_at_ms=now_ms_utc(),
        )
        if golden_run is None:
            raced_command = await repo.get_golden_run_by_command(conn, command_id)
            if raced_command is not None:
                if raced_command.command_sha256 != command_sha256:
                    raise CommandConflictError("That command id was already used for a different designation.")
                golden_run = raced_command
            else:
                golden_run = await repo.get_golden_run_by_source(conn, source_run_id)
                assert golden_run is not None
        return await _load_dossier(conn, golden_run)


async def get_dossier(conn: asyncpg.Connection, golden_run_id: int) -> GoldenValidationDossier | None:
    golden_run = await repo.get_golden_run(conn, golden_run_id)
    return None if golden_run is None else await _load_dossier(conn, golden_run)


async def list_dossiers(
    conn: asyncpg.Connection,
    *,
    strategy_name: str | None,
    symbol: str | None,
    limit: int,
) -> list[GoldenValidationDossier]:
    rows = await repo.list_golden_runs(conn, strategy_name=strategy_name, symbol=symbol, limit=limit)
    return [await _load_dossier(conn, row) for row in rows]


async def find_deployment_scope_dossiers(
    conn: asyncpg.Connection,
    *,
    strategy_name: str,
    symbol: str,
    parameters: Mapping[str, Any],
) -> GoldenDeploymentCandidates:
    """Find every exact case without coupling admission to history pagination."""
    rows = await repo.list_golden_runs_for_deployment_scope(
        conn,
        strategy_name=strategy_name,
        symbol=symbol,
        parameters_json=_canonical_json(parameters),
    )
    has_any = bool(rows) or await repo.golden_runs_exist_for_strategy(conn, strategy_name)
    return GoldenDeploymentCandidates(
        dossiers=tuple([await _load_dossier(conn, row) for row in rows]),
        strategy_has_golden_runs=has_any,
    )


async def list_latest_accepted_dossiers(
    conn: asyncpg.Connection,
    *,
    symbol: str | None,
) -> list[GoldenValidationDossier]:
    """Load accepted catalog candidates without per-case query fan-out.

    A deploy view only needs each candidate's current review, not its whole
    review history.  The repository projects that review with each Golden
    case, then this service retrieves all parity verdicts in one set query.
    The resulting evidence is identical to ``_load_dossier`` for the retained
    latest review while the round-trip count stays constant as the corpus
    grows.
    """
    catalog_rows = await repo.list_latest_accepted_golden_catalog_rows(conn, symbol=symbol)
    parsed_cases = [(row, json.loads(row.golden_run.validation_case_json)) for row in catalog_rows]
    parity_group_ids = [
        parity_group_id
        for _, validation_case in parsed_cases
        if isinstance((parity_group_id := validation_case.get("parity_group_id")), str) and parity_group_id
    ]
    verdicts = await repo.parity_verdicts_for_cases(conn, parity_group_ids)
    return [
        GoldenValidationDossier(
            golden_run=row.golden_run,
            validation_case=validation_case,
            evidence=_evidence_for(
                row.golden_run,
                verdicts.get(validation_case.get("parity_group_id")),
            ),
            reviews=(row.latest_review,),
        )
        for row, validation_case in parsed_cases
    ]


async def review(
    conn: asyncpg.Connection,
    *,
    golden_run_id: int,
    command_id: str,
    expected_evidence_revision: str,
    decision: Decision,
    reason: str,
    quantconnect_backtest_id: str | None,
    authorized_program_version: str | None,
    actor: str,
) -> GoldenValidationDossier:
    """Append judgment against exactly the evidence revision the human saw."""
    command = {
        "action": "review",
        "golden_run_id": golden_run_id,
        "expected_evidence_revision": expected_evidence_revision,
        "decision": decision,
        "reason": reason,
        "quantconnect_backtest_id": quantconnect_backtest_id,
        "authorized_program_version": authorized_program_version,
    }
    command_sha256 = _command_hash(command)
    async with conn.transaction():
        prior = await repo.get_review_by_command(conn, command_id)
        if prior is not None:
            if prior.command_sha256 != command_sha256:
                raise CommandConflictError("That command id was already used for a different review.")
            golden_run = await repo.get_golden_run(conn, prior.golden_run_id)
            assert golden_run is not None
            return await _load_dossier(conn, golden_run)

        golden_run = await repo.get_golden_run(conn, golden_run_id)
        if golden_run is None:
            raise GoldenRunNotFoundError(f"Validation Golden Run {golden_run_id} was not found.")
        dossier = await _load_dossier(conn, golden_run, lock_evidence=True)
        if dossier.evidence.revision != expected_evidence_revision:
            raise StaleEvidenceError(dossier.evidence.revision)

        case_strategy = dossier.validation_case.get("strategy")
        case_program_version = (
            case_strategy.get("program_version") if isinstance(case_strategy, dict) else None
        )
        if decision == "accept" and not case_program_version and not authorized_program_version:
            raise GoldenRunIneligibleError(
                "This historical run did not record a program version. Enter the exact version being authorized."
            )
        if decision == "reject" and authorized_program_version is not None:
            raise GoldenRunIneligibleError(
                "A rejected review cannot authorize a program version."
            )
        if case_program_version and authorized_program_version is not None:
            raise GoldenRunIneligibleError(
                "The source run already records its program version; an authorization override is not applicable."
            )

        classification: str | None = None
        if decision == "accept":
            classification = {
                "agreement": "engine_agreement",
                "deviations": "reviewed_deviations",
            }.get(dossier.evidence.state, "manual_override")
        evidence_json = _canonical_json(dossier.evidence.payload)
        inserted = await repo.insert_review(
            conn,
            golden_run_id=golden_run_id,
            command_id=command_id,
            command_sha256=command_sha256,
            expected_evidence_revision=expected_evidence_revision,
            decision=decision,
            classification=classification,
            evidence_state=dossier.evidence.state,
            parity_verdict_id=dossier.evidence.parity_verdict_id,
            evidence_json=evidence_json,
            evidence_sha256=_sha256(dossier.evidence.payload),
            reason=reason,
            quantconnect_backtest_id=quantconnect_backtest_id,
            authorized_program_version=authorized_program_version,
            reviewed_by=actor,
            reviewed_at_ms=now_ms_utc(),
        )
        if inserted is None:
            raced = await repo.get_review_by_command(conn, command_id)
            assert raced is not None
            if raced.command_sha256 != command_sha256:
                raise CommandConflictError("That command id was already used for a different review.")
        return await _load_dossier(conn, golden_run)


def assess(dossier: GoldenValidationDossier, proposed_configuration: dict[str, Any]) -> ApplicabilityReceipt:
    """Assess one accepted record against a proposed exact configuration.

    This pure boundary is reusable by Start/Resume without moving any matching
    logic into a UI. Broker, custody, arming, corpus, and every other
    operational gate remain independent of this receipt.
    """
    case = dossier.validation_case
    strategy = case.get("strategy") if isinstance(case.get("strategy"), dict) else {}
    review = dossier.latest_review
    authorized_program_version = (
        review.authorized_program_version
        if review is not None and review.decision == "accept"
        else None
    )
    expected = {
        "strategy_name": strategy.get("name"),
        "program_version": strategy.get("program_version") or authorized_program_version,
        "symbol": case.get("symbol"),
        "parameters": case.get("parameters"),
        "window": case.get("window"),
        "data_policy": case.get("data_policy"),
        "execution": case.get("execution"),
    }
    mismatches = tuple(field for field, value in expected.items() if proposed_configuration.get(field) != value)
    classification = review.classification if review is not None and review.decision == "accept" else None
    if mismatches:
        explanation = "The proposed configuration differs from this Golden Validation case."
    elif review is None:
        explanation = "This Golden Validation candidate has not been reviewed."
    elif review.decision != "accept":
        explanation = "The latest human review rejected this Golden Validation case."
    elif dossier.review_is_current is not True:
        explanation = "Computed evidence changed after the latest human review; review the new evidence revision."
    else:
        explanation = (
            "This accepted Golden Validation matches the proposed configuration exactly. "
            "All independent Paper or Live safety gates still apply."
        )
    applicable = not mismatches and review is not None and review.decision == "accept" and dossier.review_is_current is True
    return ApplicabilityReceipt(
        golden_validation_id=dossier.golden_run.id,
        applicable=applicable,
        state=dossier.state,
        classification=classification,
        mismatched_fields=mismatches,
        explanation=explanation,
    )


def assess_deployment_scope(
    dossier: GoldenValidationDossier,
    proposed_configuration: dict[str, Any],
) -> ApplicabilityReceipt:
    """Project applicability onto the four facts a running bot can carry.

    The historical window, input-data identity, and backtest execution model
    remain frozen provenance on the selected case.  A Paper/Live run cannot
    truthfully claim those are its own future window or broker execution
    model, so admission matches only the deployable strategy identity: exact
    program version, symbol, and fully resolved parameters.
    """
    case = dossier.validation_case
    strategy = case.get("strategy") if isinstance(case.get("strategy"), dict) else {}
    review = dossier.latest_review
    authorized_program_version = (
        review.authorized_program_version
        if review is not None and review.decision == "accept"
        else None
    )
    expected = {
        "strategy_name": strategy.get("name"),
        "program_version": strategy.get("program_version") or authorized_program_version,
        "symbol": case.get("symbol"),
        "parameters": case.get("parameters"),
    }
    mismatches = tuple(field for field, value in expected.items() if proposed_configuration.get(field) != value)
    classification = review.classification if review is not None and review.decision == "accept" else None
    if mismatches:
        explanation = "The proposed deployment differs from this Golden Validation scope."
    elif review is None:
        explanation = "This Golden Validation candidate has not been reviewed."
    elif review.decision != "accept":
        explanation = "The latest human review rejected this Golden Validation case."
    elif dossier.review_is_current is not True:
        explanation = "Computed evidence changed after the latest human review; review the new evidence revision."
    else:
        explanation = (
            "This accepted Golden Validation matches the exact program, symbol, and resolved parameters. "
            "Its historical data window and execution assumptions remain frozen evidence provenance, and all "
            "independent Paper or Live safety gates still apply."
        )
    applicable = not mismatches and review is not None and review.decision == "accept" and dossier.review_is_current is True
    return ApplicabilityReceipt(
        golden_validation_id=dossier.golden_run.id,
        applicable=applicable,
        state=dossier.state,
        classification=classification,
        mismatched_fields=mismatches,
        explanation=explanation,
    )
