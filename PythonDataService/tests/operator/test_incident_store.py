"""Tests for IncidentStore — atomic per-run writes + unresolved reader."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.operator.incidents.store import INCIDENTS_DIR, IncidentStore
from app.operator.notices.schema import OperatorIncident, OperatorNotice, OperatorNoticeAction

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _baseline_notice(*, code: str = "watchdog.flatten_completed") -> OperatorNotice:
    return OperatorNotice(
        code=code,  # type: ignore[arg-type]
        tier="info",
        title="Flatten completed",
        message="The engine flattened all positions cleanly.",
        action=OperatorNoticeAction(kind="none"),
    )


def _make_incident(
    incident_id: str = "inc-1",
    *,
    started_at_ms: int = 1_700_000_000_000,
    resolved_at_ms: int | None = None,
    notice_code: str = "watchdog.flatten_completed",
) -> OperatorIncident:
    return OperatorIncident(
        incident_id=incident_id,
        category="watchdog",
        notice=_baseline_notice(code=notice_code),
        started_at_ms=started_at_ms,
        resolved_at_ms=resolved_at_ms,
    )


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------


def test_append_writes_atomically(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    incident = _make_incident()

    written = store.append(incident)

    assert written.exists()
    assert not written.with_suffix(written.suffix + ".tmp").exists()
    loaded = OperatorIncident.model_validate_json(written.read_text(encoding="utf-8"))
    assert loaded == incident


def test_append_creates_incidents_subdir(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    store.append(_make_incident("inc-1"))
    assert (tmp_path / INCIDENTS_DIR).is_dir()


def test_append_returns_correct_path(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    path = store.append(_make_incident("my-incident"))
    assert path == tmp_path / INCIDENTS_DIR / "my-incident.json"


def test_append_multiple_incidents(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    store.append(_make_incident("inc-1"))
    store.append(_make_incident("inc-2"))
    files = list((tmp_path / INCIDENTS_DIR).glob("*.json"))
    assert len(files) == 2


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_amends_resolved_at_ms(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    store.append(_make_incident("inc-1"))

    store.resolve("inc-1", resolved_at_ms=1_700_000_100_000)

    path = tmp_path / INCIDENTS_DIR / "inc-1.json"
    loaded = OperatorIncident.model_validate_json(path.read_text(encoding="utf-8"))
    assert loaded.resolved_at_ms == 1_700_000_100_000


def test_resolve_preserves_other_fields(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    original = _make_incident("inc-1", started_at_ms=1_700_000_000_123)
    store.append(original)

    store.resolve("inc-1", resolved_at_ms=1_700_000_100_000)

    path = tmp_path / INCIDENTS_DIR / "inc-1.json"
    loaded = OperatorIncident.model_validate_json(path.read_text(encoding="utf-8"))
    assert loaded.started_at_ms == 1_700_000_000_123
    assert loaded.incident_id == "inc-1"


def test_resolve_missing_incident_raises(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.resolve("nonexistent", resolved_at_ms=1_700_000_100_000)


# ---------------------------------------------------------------------------
# list_unresolved
# ---------------------------------------------------------------------------


def test_list_unresolved_empty_dir_returns_empty(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    assert store.list_unresolved() == []


def test_list_unresolved_missing_dir_returns_empty(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path / "no-such-run")
    assert store.list_unresolved() == []


def test_list_unresolved_excludes_resolved(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    store.append(_make_incident("inc-1", resolved_at_ms=None))
    store.append(_make_incident("inc-2", resolved_at_ms=1_700_000_100_000))

    unresolved = store.list_unresolved()
    assert [i.incident_id for i in unresolved] == ["inc-1"]


def test_list_unresolved_returns_all_unresolved(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    store.append(_make_incident("inc-1"))
    store.append(_make_incident("inc-2"))
    store.append(_make_incident("inc-3", resolved_at_ms=1_700_000_200_000))

    unresolved = store.list_unresolved()
    assert {i.incident_id for i in unresolved} == {"inc-1", "inc-2"}


# ---------------------------------------------------------------------------
# Atomicity under concurrent writers
# ---------------------------------------------------------------------------


def test_append_is_atomic_under_concurrent_writers(tmp_path: Path) -> None:
    """Two threads appending different incidents at the same instant;
    both succeed; no partial files are observed."""
    store = IncidentStore(tmp_path)
    errors: list[Exception] = []

    def _write(idx: int) -> None:
        try:
            store.append(_make_incident(f"inc-{idx}"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent writes raised: {errors}"
    files = list((tmp_path / INCIDENTS_DIR).glob("*.json"))
    assert len(files) == 10
    # No leftover .tmp files
    tmp_files = list((tmp_path / INCIDENTS_DIR).glob("*.tmp"))
    assert tmp_files == []
