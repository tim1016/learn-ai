"""Append-only authorization records for paper-only developer clean slates."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

from app.broker.alpaca.clerk.sqlite.operational_files import confined_relative_path
from app.broker.alpaca.paths import fsync_directory

DEVELOPER_RESET_REGISTRY_FILENAME = "_developer_clean_slate_resets.jsonl"
_RESET_FIELDS = frozenset(
    {
        "account_id",
        "prior_authority_generation",
        "recorded_at_ms",
        "manifest_reference",
        "manifest_sha256",
    }
)


@dataclass(frozen=True)
class DeveloperCleanSlateReset:
    """One audited authorization to reinitialize a moved-aside generation."""

    account_id: str
    prior_authority_generation: int
    recorded_at_ms: int
    manifest_reference: str
    manifest_sha256: str


class DeveloperCleanSlateResetRegistryInvalid(ValueError):
    """A reset authorization ledger record is malformed or untrusted."""


class DeveloperCleanSlateResetRegistry:
    """Append-only reset ledger kept outside any single account directory."""

    def __init__(self, alpaca_accounts_root: Path) -> None:
        self._path = alpaca_accounts_root / DEVELOPER_RESET_REGISTRY_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def record(self, reset: DeveloperCleanSlateReset) -> None:
        """Durably publish one reset authorization without rewriting history."""
        existed = self._path.is_file()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(reset), sort_keys=True, separators=(",", ":")) + "\n"
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            fsync_directory(self._path.parent)

    def latest(self, account_id: str) -> DeveloperCleanSlateReset | None:
        """Return the latest reset authorization for exactly one account."""
        if not self._path.is_file():
            return None
        latest: DeveloperCleanSlateReset | None = None
        for reset in self._records():
            if reset.account_id == account_id:
                latest = reset
        return latest

    def contains(self, expected: DeveloperCleanSlateReset) -> bool:
        """Return whether this exact reset authorization was already published."""
        if not self._path.is_file():
            return False
        return any(reset == expected for reset in self._records())

    def authorizes_reinitialize(
        self,
        *,
        account_id: str,
        prior_authority_generation: int,
        artifacts_root: Path,
    ) -> bool:
        """Verify the linked manifest before allowing a fresh generation."""
        try:
            reset = self.latest(account_id)
        except DeveloperCleanSlateResetRegistryInvalid:
            return False
        if reset is None or reset.prior_authority_generation != prior_authority_generation:
            return False
        try:
            manifest_path = confined_relative_path(artifacts_root, reset.manifest_reference)
        except ValueError:
            return False
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return False
        try:
            raw = manifest_path.read_bytes()
            manifest = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError):
            return False
        return (
            sha256(raw).hexdigest() == reset.manifest_sha256
            and isinstance(manifest, dict)
            and manifest.get("schema_version") == 1
            and manifest.get("operation") == "DEV_CLEAN_SLATE_RESET"
            and manifest.get("account_id") == account_id
            and manifest.get("recorded_at_ms") == reset.recorded_at_ms
            and manifest.get("prior_authority_generation") == prior_authority_generation
        )

    def _records(self) -> tuple[DeveloperCleanSlateReset, ...]:
        try:
            with self._path.open(encoding="utf-8") as handle:
                return tuple(
                    _decode_reset_record(raw, line_number=line_number)
                    for line_number, raw in enumerate(handle, start=1)
                    if raw.strip()
                )
        except OSError as exc:
            raise DeveloperCleanSlateResetRegistryInvalid(
                f"developer reset ledger cannot be read: {exc}"
            ) from exc


def _decode_reset_record(raw: str, *, line_number: int) -> DeveloperCleanSlateReset:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeveloperCleanSlateResetRegistryInvalid(
            f"developer reset ledger line {line_number} is not valid JSON"
        ) from exc
    if not isinstance(payload, dict) or frozenset(payload) != _RESET_FIELDS:
        raise DeveloperCleanSlateResetRegistryInvalid(
            f"developer reset ledger line {line_number} has invalid fields"
        )
    account_id = payload["account_id"]
    generation = payload["prior_authority_generation"]
    recorded_at_ms = payload["recorded_at_ms"]
    manifest_reference = payload["manifest_reference"]
    manifest_sha256 = payload["manifest_sha256"]
    if (
        not isinstance(account_id, str)
        or type(generation) is not int
        or type(recorded_at_ms) is not int
        or not isinstance(manifest_reference, str)
        or not isinstance(manifest_sha256, str)
    ):
        raise DeveloperCleanSlateResetRegistryInvalid(
            f"developer reset ledger line {line_number} has invalid value types"
        )
    return DeveloperCleanSlateReset(
        account_id=account_id,
        prior_authority_generation=generation,
        recorded_at_ms=recorded_at_ms,
        manifest_reference=manifest_reference,
        manifest_sha256=manifest_sha256,
    )
