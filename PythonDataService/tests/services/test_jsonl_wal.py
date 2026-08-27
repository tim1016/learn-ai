"""Path-confinement tests for the shared JSONL WAL primitive.

``JsonlWal`` refuses to touch a file that resolves outside its trusted root.
That check is the module's only defence against a path-traversal write, and
until PR-C of #1813 it was inlined identically at four sites with no test
covering any of them. These tests pin the guard at every public entry point
so the confinement is a proven property of the class rather than a property
of one particular copy of the check.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.services.jsonl_wal import JsonlWal


class _Record(BaseModel):
    seq: int
    value: str


def _corrupt_error(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"{path}: {detail}")


def _wal(path: Path, trusted_root: Path) -> JsonlWal[_Record]:
    return JsonlWal(
        path,
        record_model=_Record,
        corrupt_error=_corrupt_error,
        seq_of=lambda record: record.seq,
        label="test",
        trusted_root=trusted_root,
    )


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda wal: wal.path, id="path"),
        pytest.param(lambda wal: wal.append(_Record(seq=1, value="x")), id="append"),
        pytest.param(lambda wal: wal.read_all(), id="read_all"),
        pytest.param(lambda wal: wal.read_tail(limit=1), id="read_tail"),
        pytest.param(lambda wal: wal.read_from(after_seq=0), id="read_from"),
        pytest.param(lambda wal: wal.last_seq(), id="last_seq"),
        pytest.param(lambda wal: wal.allocate_seq(), id="allocate_seq"),
    ],
)
def test_every_entry_point_refuses_a_path_outside_the_trusted_root(
    tmp_path: Path, call: Callable[[JsonlWal[_Record]], object]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    wal = _wal(root / ".." / "outside.jsonl", root)

    with pytest.raises(ValueError, match="escapes root"):
        call(wal)

    assert not (tmp_path / "outside.jsonl").exists(), (
        "the guard raised but the escaping file was created anyway"
    )


def test_a_path_inside_the_trusted_root_round_trips(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    wal = _wal(root / "log.jsonl", root)

    wal.append(_Record(seq=1, value="first"))
    wal.append(_Record(seq=2, value="second"))

    assert [record.value for record in wal.read_all()] == ["first", "second"]
    assert wal.last_seq() == 2
    assert wal.path == (root / "log.jsonl").resolve()


def test_a_symlink_escaping_the_trusted_root_is_refused(tmp_path: Path) -> None:
    """The check resolves symlinks before comparing, so a link out is caught."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    link = root / "log.jsonl"
    link.symlink_to(outside)

    wal = _wal(link, root)

    with pytest.raises(ValueError, match="escapes root"):
        wal.append(_Record(seq=1, value="x"))

    assert outside.read_text(encoding="utf-8") == "", "the symlink target was written through"
