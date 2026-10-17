"""Approve, judge, revoke and reprove exact tested Options setups.

Saved setup names remain editable labels. The approved setup, source run,
repeatability proof and owner note are immutable, and events append changes
to qualification without rewriting a deployed bot's configuration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import uuid4

import asyncpg

from app.options_backtest import saved_runs
from app.options_backtest.run import RunResult
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_bots.golden_proof import (
    GoldenProofError,
    build_proof,
    current_rules_digest,
    proof_sha256,
    reprove,
    setup_sha256,
)
from app.options_bots.golden_repository import (
    CommandConflict,
    GoldenEvent,
    GoldenRecord,
    PostgresGoldenStore,
    SourceUnavailable,
)
from app.research.persistence.db import with_connection
from app.schemas.golden_option_setups import (
    ApproveGoldenOptionSetupRequest,
    GoldenOptionSetupEventView,
    GoldenOptionSetupView,
    ReproveGoldenOptionSetupRequest,
    RevokeGoldenOptionSetupRequest,
)
from app.schemas.signal_program_seal import semantic_payload_hash
from app.utils.timestamps import now_ms_utc

LIMITATIONS = (
    "Golden means owner-approved for this exact tested setup, not optimal or automatically searched.",
    "Repeatability on archived trade bars does not prove quote spreads, order waiting or live fills.",
    "Capital, fees and slippage are the selected backtest's assumptions; a deployment budget is a separate constraint.",
)


class GoldenSetupError(ValueError):
    def __init__(self, code: str, message: str, *, http_status: int = 409) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


class GoldenStore(Protocol):
    async def list(self, *, limit: int = 200, source_run_id: int | None = None) -> tuple[GoldenRecord, ...]: ...
    async def get(self, qualification_id: str) -> GoldenRecord | None: ...
    async def by_command(self, command_id: str) -> GoldenRecord | None: ...
    async def events(self, qualification_id: str) -> tuple[GoldenEvent, ...]: ...
    async def events_for(self, qualification_ids: Sequence[str]) -> tuple[GoldenEvent, ...]: ...
    async def event_by_command(self, command_id: str) -> GoldenEvent | None: ...
    async def insert(self, record: GoldenRecord) -> GoldenRecord: ...
    async def append(self, event: GoldenEvent) -> GoldenEvent: ...


@dataclass(frozen=True)
class SourceRun:
    name: str
    result: RunResult
    calendar_identity: str
    calendar_package_version: str


async def _source(conn: asyncpg.Connection, run_id: int) -> SourceRun:
    stored = await saved_runs.get_run(conn, run_id, is_live=saved_runs.not_asked)
    if stored is None:
        raise GoldenSetupError("RUN_NOT_FOUND", "The selected Options Backtest run does not exist.", http_status=404)
    if stored.record.status != "complete":
        raise GoldenSetupError("RUN_NOT_COMPLETE", "Only a completed Options Backtest run can be approved.")
    result = await saved_runs.read_result(conn, run_id)
    if result is None:
        raise GoldenSetupError("EVIDENCE_UNREADABLE", "The selected run's evidence cannot be read.")
    return SourceRun(
        name=stored.record.setup_name or f"{stored.record.underlying} setup from run {run_id}", result=result,
        calendar_identity=stored.record.calendar_identity, calendar_package_version=stored.record.calendar_package_version,
    )


async def load_source(run_id: int) -> SourceRun:
    return await with_connection(_source, run_id)


def qualification_status(
    record: GoldenRecord, events: Sequence[GoldenEvent], *, running_digest: str | None
) -> tuple[Literal["ready", "stale", "revoked"], str]:
    """Pure status judgment; missing build identity never grants entry permission."""
    if any(event.kind == "revoked" for event in events):
        return "revoked", "The owner revoked this Golden setup. New entries and re-entries are blocked."
    proved_digest = record.rules_digest
    for event in events:
        if event.kind == "reproved":
            proved_digest = event.rules_digest or ""
    if running_digest is None or proved_digest != running_digest:
        return "stale", "The Options rules changed or cannot be identified. Re-prove this version before new entries."
    if record.evidence.get("options_search_study_id"):
        from app.research.options_golden_search.identity import execution_identity

        try:
            identity = execution_identity()
        except (ValueError, GoldenProofError):
            identity = None
        if identity is None or record.evidence.get("execution_identity") != identity:
            return "stale", "The search qualification identity changed. Its historical verdict is retained; a new study approval is required."
    return "ready", "This exact tested setup is owner-approved and its repeatability proof matches the running rules."


class GoldenOptionsSetups:
    def __init__(
        self,
        store: GoldenStore | None = None,
        *,
        source: Callable[[int], Awaitable[SourceRun]] = load_source,
        prove: Callable[[RunResult], dict[str, Any]] = build_proof,
        reprove_run: Callable[[RunResult, dict[str, Any]], dict[str, Any]] = reprove,
        rules_digest: Callable[[], str] = current_rules_digest,
    ) -> None:
        self.store = store or PostgresGoldenStore()
        self.source = source
        self.prove = prove
        self.reprove_run = reprove_run
        self.rules_digest = rules_digest

    async def _view(self, record: GoldenRecord) -> GoldenOptionSetupView:
        events = await self.store.events(record.id)
        return self._project(record, events, running_digest=await self._running_digest())

    async def _running_digest(self) -> str | None:
        try:
            return await asyncio.to_thread(self.rules_digest)
        except GoldenProofError:
            return None

    @staticmethod
    def _project(record: GoldenRecord, events: Sequence[GoldenEvent], *, running_digest: str | None) -> GoldenOptionSetupView:
        setup = OptionsBacktestSetup.model_validate(record.setup)
        if setup_sha256(setup) != record.setup_sha256 or proof_sha256(record.proof) != record.proof_sha256:
            raise GoldenSetupError("EVIDENCE_UNREADABLE", "The retained approval does not match its recorded identity.")
        status, explanation = qualification_status(record, events, running_digest=running_digest)
        effective_proof = record.proof
        for event in events:
            if event.kind == "reproved" and event.proof is not None:
                effective_proof = event.proof
        return GoldenOptionSetupView(
            id=record.id, name=record.name, underlying=setup.underlying, setup=setup,
            setup_sha256=record.setup_sha256, source_run_id=record.source_run_id, rules_digest=effective_proof["rules_digest"],
            proof_sha256=proof_sha256(effective_proof), status=status, status_explanation=explanation,
            created_at_ms=record.created_at_ms, approved_by=record.approved_by, note=record.note,
            evidence=record.evidence, proof=effective_proof,
            events=[
                GoldenOptionSetupEventView(
                    id=event.id, kind=event.kind, rules_digest=event.rules_digest, reason=event.reason,
                    actor=event.actor, created_at_ms=event.created_at_ms,
                ) for event in events
            ],
        )

    async def list(self, *, limit: int = 200, source_run_id: int | None = None) -> list[GoldenOptionSetupView]:
        records = await self.store.list(limit=limit, source_run_id=source_run_id)
        if not records:
            return []
        history, running = await asyncio.gather(self.store.events_for([record.id for record in records]), self._running_digest())
        events: dict[str, list[GoldenEvent]] = {record.id: [] for record in records}
        for event in history:
            events[event.qualification_id].append(event)
        return [self._project(record, events[record.id], running_digest=running) for record in records]

    async def _get(self, qualification_id: str) -> GoldenRecord:
        record = await self.store.get(qualification_id)
        if record is None:
            raise GoldenSetupError("GOLDEN_SETUP_NOT_FOUND", "This Golden Options setup does not exist.", http_status=404)
        return record

    async def get(self, qualification_id: str) -> GoldenOptionSetupView:
        return await self._view(await self._get(qualification_id))

    async def ready(self, qualification_id: str) -> GoldenOptionSetupView:
        view = await self.get(qualification_id)
        if view.status != "ready":
            raise GoldenSetupError(f"GOLDEN_SETUP_{view.status.upper()}", view.status_explanation)
        return view

    async def approve(self, request: ApproveGoldenOptionSetupRequest) -> GoldenOptionSetupView:
        command_hash = semantic_payload_hash(request.model_dump(mode="json", exclude={"idempotency_key"}))
        prior = await self.store.by_command(request.idempotency_key)
        if prior is not None:
            if prior.command_sha256 != command_hash:
                raise GoldenSetupError("COMMAND_CONFLICT", "This approval key already names a different request.")
            return await self._view(prior)
        source = await self.source(request.run_id)
        digest = setup_sha256(source.result.setup)
        if request.expected_setup_sha256 is not None and request.expected_setup_sha256 != digest:
            raise GoldenSetupError("SETUP_CHANGED", "The selected run's setup is different from the setup reviewed.")
        try:
            proof = await asyncio.to_thread(self.prove, source.result)
        except GoldenProofError as exc:
            raise GoldenSetupError("PROOF_MISMATCH", str(exc)) from exc
        if proof["calendar_identity"] != source.calendar_identity or (
            proof["calendar_package_version"] != source.calendar_package_version
        ):
            raise GoldenSetupError("RULES_STALE", "The selected run used a different calendar. Run the setup again.")
        result = source.result
        evidence = {
            "first_session_open_ms": result.sessions[0].session_open_ms,
            "last_session_open_ms": result.sessions[-1].session_open_ms,
            "sessions_tested": result.summary.sessions_tested, "trades": result.summary.trades,
            "net_profit_usd": str(result.summary.net_profit_usd),
            "starting_cash_usd": str(result.setup.starting_cash_usd),
            "slippage_percent": str(result.setup.slippage_percent), "limitations": list(LIMITATIONS),
        }
        record = GoldenRecord(
            id=f"ogs_{uuid4().hex}", name=source.name, source_run_id=request.run_id,
            setup=result.setup.model_dump(mode="json"), setup_sha256=digest,
            rules_digest=proof["rules_digest"], proof=proof, proof_sha256=proof_sha256(proof), evidence=evidence,
            note=request.note, approved_by="owner", created_at_ms=now_ms_utc(),
            command_id=request.idempotency_key, command_sha256=command_hash,
        )
        try:
            stored = await self.store.insert(record)
        except CommandConflict as exc:
            raise GoldenSetupError("COMMAND_CONFLICT", str(exc)) from exc
        except SourceUnavailable as exc:
            raise GoldenSetupError("EVIDENCE_UNAVAILABLE", str(exc)) from exc
        return await self._view(stored)

    async def _event_retry(self, key: str, command_hash: str) -> GoldenOptionSetupView | None:
        prior = await self.store.event_by_command(key)
        if prior is None:
            return None
        if prior.command_sha256 != command_hash:
            raise GoldenSetupError("COMMAND_CONFLICT", "This event key already names a different request.")
        return await self.get(prior.qualification_id)

    async def revoke(self, qualification_id: str, request: RevokeGoldenOptionSetupRequest) -> GoldenOptionSetupView:
        command_hash = semantic_payload_hash({"id": qualification_id, "kind": "revoked", "reason": request.reason})
        if retried := await self._event_retry(request.idempotency_key, command_hash):
            return retried
        await self._get(qualification_id)
        event = GoldenEvent(
            0, qualification_id, "revoked", None, None, request.reason, "owner", now_ms_utc(),
            request.idempotency_key, command_hash,
        )
        try:
            await self.store.append(event)
        except CommandConflict as exc:
            raise GoldenSetupError("COMMAND_CONFLICT", str(exc)) from exc
        return await self.get(qualification_id)

    async def reprove(self, qualification_id: str, request: ReproveGoldenOptionSetupRequest) -> GoldenOptionSetupView:
        command_hash = semantic_payload_hash({"id": qualification_id, "kind": "reproved"})
        if retried := await self._event_retry(request.idempotency_key, command_hash):
            return retried
        record = await self._get(qualification_id)
        view = await self._view(record)
        if view.status == "revoked":
            raise GoldenSetupError("GOLDEN_SETUP_REVOKED", "Approve a new version instead of re-proving a revoked setup.")
        source = await self.source(record.source_run_id)
        proof = record.proof
        try:
            proved = await asyncio.to_thread(self.reprove_run, source.result, proof)
        except GoldenProofError as exc:
            raise GoldenSetupError("PROOF_MISMATCH", str(exc)) from exc
        event = GoldenEvent(
            0, qualification_id, "reproved", proved["rules_digest"], proved, None, "owner", now_ms_utc(),
            request.idempotency_key, command_hash,
        )
        try:
            await self.store.append(event)
        except CommandConflict as exc:
            raise GoldenSetupError("COMMAND_CONFLICT", str(exc)) from exc
        except ValueError as exc:
            raise GoldenSetupError("GOLDEN_SETUP_REVOKED", str(exc)) from exc
        return await self.get(qualification_id)


async def get_ready_setup(qualification_id: str) -> GoldenOptionSetupView:
    """Deploy/runtime admission: unavailable, stale or revoked never permits an entry."""
    return await GoldenOptionsSetups().ready(qualification_id)
