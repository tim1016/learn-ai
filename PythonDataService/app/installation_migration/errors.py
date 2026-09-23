"""The one refusal family of ``migrate-installation`` (#2268).

Every refusal is loud: a bounded snake_case ``reason``, an operator-readable
``message`` that names the account, volume or path it is about, and the same
subject repeated as structured ``details`` so the CLI's JSON line carries it
without anyone parsing prose.
"""

from __future__ import annotations

from collections.abc import Mapping


class MigrationRefused(Exception):
    """The migration will not proceed, and says exactly why."""

    def __init__(
        self, reason: str, message: str, *, details: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details: dict[str, object] = dict(details or {})

    def to_json(self) -> dict[str, object]:
        """The refusal's JSON-line shape."""
        return {
            "error": f"{self.reason}: {self.message}",
            "reason": self.reason,
            "details": self.details,
        }


__all__ = ["MigrationRefused"]
