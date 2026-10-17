"""Durable owner recovery keys; replaying one never starts a second run (ADR 0079)."""

from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field

from app.engine.live.durable_append_log import create_atomic_exclusive_durable_file
from app.schemas.options_deploy import OptionsResumeRequest
from app.schemas.signal_program_seal import semantic_payload_hash
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class RecoveryCommand(BaseModel):
    run_id: str
    prior_run_id: str
    request_sha256: str
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)


def claim_recovery(
    instance_dir: Path, *, prior_run_id: str, request: OptionsResumeRequest, now_ms: int,
) -> RecoveryCommand:
    directory = instance_dir / "options_resume_commands"
    directory.mkdir(parents=True, exist_ok=True)
    payload = request.model_dump(mode="json")
    if payload["budget"] is not None:
        payload["budget"].pop("review_token", None)
        payload["budget"].pop("live_confirmation", None)
    digest = semantic_payload_hash(payload)
    key = semantic_payload_hash({"key": request.idempotency_key})
    path = directory / f"{key}.json"
    command = RecoveryCommand(
        run_id=uuid5(NAMESPACE_URL, f"{instance_dir.name}:options-resume:{key}").hex,
        prior_run_id=prior_run_id, request_sha256=digest, created_at_ms=now_ms,
    )
    try:
        create_atomic_exclusive_durable_file(path, command.model_dump_json(), trusted_root=instance_dir)
    except FileExistsError:
        command = RecoveryCommand.model_validate_json(path.read_text(encoding="utf-8"))
        if command.request_sha256 != digest:
            raise ValueError("This Resume key already names a different recovery request.") from None
    return command
