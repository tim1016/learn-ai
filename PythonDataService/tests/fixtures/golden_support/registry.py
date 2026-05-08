"""Fixture registry — lookup fixtures by ID and resolve file paths.

The registry loads manifest.json once (lazy, on first access) and caches
it for the lifetime of the process. Tests should call registry.get() to
locate fixtures rather than hard-coding paths, so the active_version
pointer in the manifest controls which fixture version a test sees.

Usage:
    from golden_support.registry import Registry

    reg = Registry()
    fixture = reg.get("BS-001")               # Fixture model
    files = reg.active_files("BS-001")         # FixtureFiles model
    input_path = reg.resolve("BS-001", "input")  # Path to input.arrow
"""
from __future__ import annotations

from functools import cached_property
from pathlib import Path

from .manifest import MANIFEST_PATH, Fixture, FixtureFiles, Manifest

GOLDEN_ROOT = MANIFEST_PATH.parent


class FixtureNotFoundError(KeyError):
    """Raised when a fixture ID is not in the manifest."""


class VersionFileNotFoundError(FileNotFoundError):
    """Raised when a fixture file referenced by the manifest does not exist on disk."""


class Registry:
    """Fixture registry backed by manifest.json.

    Instantiate once per test module or use the module-level ``default``
    instance. Multiple Registry instances pointing to the same manifest
    are equivalent; they each load and cache the manifest independently.
    """

    def __init__(self, manifest_path: Path = MANIFEST_PATH) -> None:
        self._manifest_path = manifest_path
        self._root = manifest_path.parent

    @cached_property
    def _manifest(self) -> Manifest:
        return Manifest.load(self._manifest_path)

    def reload(self) -> None:
        """Force a reload of the manifest from disk (clears the cache)."""
        if "_manifest" in self.__dict__:
            del self.__dict__["_manifest"]

    def all(self) -> list[Fixture]:
        """Return all fixtures in the manifest."""
        return list(self._manifest.fixtures)

    def get(self, fixture_id: str) -> Fixture:
        """Return the Fixture for ``fixture_id``, or raise FixtureNotFoundError."""
        fixture = self._manifest.by_id(fixture_id)
        if fixture is None:
            known = [f.id for f in self._manifest.fixtures]
            raise FixtureNotFoundError(
                f"Fixture {fixture_id!r} not in manifest. "
                f"Known IDs: {known}"
            )
        return fixture

    def active_files(self, fixture_id: str) -> FixtureFiles:
        """Return the FixtureFiles for the active version of ``fixture_id``."""
        fixture = self.get(fixture_id)
        files = fixture.active_files
        if files is None:
            raise FixtureNotFoundError(
                f"Fixture {fixture_id!r}: active_version={fixture.active_version} "
                f"has no FixtureFiles entry."
            )
        return files

    def fixture_dir(self, fixture_id: str) -> Path:
        """Return the versioned directory for the active version of ``fixture_id``."""
        fixture = self.get(fixture_id)
        # Convention: <golden_root>/<category>/<id>/v<version>/
        return (
            self._root
            / fixture.category
            / fixture.id
            / f"v{fixture.active_version}"
        )

    def resolve(self, fixture_id: str, file_key: str) -> Path:
        """Return the Path for a file in the active fixture version.

        ``file_key`` must be one of the keys on FixtureFiles: "input",
        "output", or "attribution".

        Raises VersionFileNotFoundError if the file does not exist on disk.
        """
        files = self.active_files(fixture_id)
        filename: str | None = getattr(files, file_key, None)
        if filename is None:
            valid = ["input", "output", "attribution"]
            raise ValueError(
                f"file_key={file_key!r} is not a valid fixture file key. "
                f"Valid keys: {valid}"
            )
        path = self.fixture_dir(fixture_id) / filename
        if not path.exists():
            raise VersionFileNotFoundError(
                f"Fixture {fixture_id!r} active file {file_key!r} not found: {path}"
            )
        return path

    def exists_on_disk(self, fixture_id: str) -> bool:
        """Return True if all active version files exist on disk."""
        try:
            fixture = self.get(fixture_id)
            files = fixture.active_files
            if files is None:
                return False
            for key in ("input", "output", "attribution"):
                path = self.fixture_dir(fixture_id) / getattr(files, key)
                if not path.exists():
                    return False
            return True
        except (FixtureNotFoundError, VersionFileNotFoundError):
            return False


# Module-level default registry (loads on first use)
default = Registry()
