"""Path-confinement tests for the shared JSONL WAL primitive.

``JsonlWal`` refuses to touch a file that resolves outside its trusted root.
That check is the module's only defence against a path-traversal write, and
until PR-C of #1813 it was inlined identically at four sites with no test
covering any of them. These tests pin the guard at every public entry point
so the confinement is a proven property of the class rather than a property
of one particular copy of the check.
"""

from __future__ import annotations

import os
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
        pytest.param(lambda wal: wal.append_many([_Record(seq=1, value="x")]), id="append_many"),
        pytest.param(lambda wal: wal.replace_all([_Record(seq=1, value="x")]), id="replace_all"),
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


def test_several_records_appended_at_once_read_back_in_order_and_the_next_one_follows_on(tmp_path: Path) -> None:
    wal = _wal(tmp_path / "wal.jsonl", tmp_path)
    wal.append(_Record(seq=1, value="a"))

    wal.append_many([_Record(seq=2, value="b"), _Record(seq=3, value="c")])
    wal.append(_Record(seq=4, value="d"))

    assert [record.value for record in wal.read_all()] == ["a", "b", "c", "d"]
    with pytest.raises(ValueError, match="got seq=7 but next available seq is 6"):
        wal.append_many([_Record(seq=5, value="e"), _Record(seq=7, value="g")])
    assert [record.seq for record in wal.read_all()] == [1, 2, 3, 4], "a refused batch wrote nothing"


def test_several_records_appended_after_a_torn_line_replace_it(tmp_path: Path) -> None:
    """A crash left half a line. The next append, of one record or of several, truncates it first."""
    path = tmp_path / "wal.jsonl"
    wal = _wal(path, tmp_path)
    wal.append(_Record(seq=1, value="a"))
    with open(path, "a", encoding="utf-8") as torn:
        torn.write('{"seq":2,"val')

    wal.append_many([_Record(seq=2, value="b"), _Record(seq=3, value="c")])

    assert [record.value for record in wal.read_all()] == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("kept", "next_seq"),
    [
        pytest.param((2, 6), 7, id="a_rewrite_leaves_exactly_the_records_given_under_the_sequences_they_had"),
        pytest.param(
            (1, 2),
            3,
            id="after_a_rewrite_that_drops_the_tail_the_next_sequence_is_the_one_a_fresh_reader_of_the_file_gives",
        ),
    ],
)
def test_a_rewrite_keeps_the_given_sequences_and_the_next_one_runs_on_from_the_last(
    tmp_path: Path, kept: tuple[int, ...], next_seq: int
) -> None:
    path = tmp_path / "wal.jsonl"
    wal = _wal(path, tmp_path)
    wal.append_many([_Record(seq=seq, value=str(seq)) for seq in range(1, 7)])

    wal.replace_all([_Record(seq=seq, value=str(seq)) for seq in kept])

    assert [record.seq for record in wal.read_all()] == list(kept), "the rewrite leaves exactly the records given"
    # The sequence runs on from the last one kept: it never reuses one. The writer that rewrote and a
    # process started afterwards must agree, or one of them refuses the other's append.
    assert wal.allocate_seq() == _wal(path, tmp_path).allocate_seq() == next_seq, "next sequence, writer and fresh reader"
    wal.append(_Record(seq=next_seq, value=str(next_seq)))
    assert [record.seq for record in _wal(path, tmp_path).read_all()] == [*kept, next_seq], "a fresh reader reads the kept records and the append"
    assert [entry.name for entry in tmp_path.iterdir() if entry.name.endswith(".tmp")] == [], "no temp file is left behind"


def test_a_rewrite_refuses_sequences_that_do_not_rise_and_leaves_the_file_as_it_was(tmp_path: Path) -> None:
    wal = _wal(tmp_path / "wal.jsonl", tmp_path)
    wal.append_many([_Record(seq=1, value="a"), _Record(seq=2, value="b")])

    with pytest.raises(ValueError, match="sequences must rise"):
        wal.replace_all([_Record(seq=2, value="b"), _Record(seq=1, value="a")])

    assert [record.seq for record in wal.read_all()] == [1, 2]


class _Stopped(BaseException):
    """The process is stopped: nothing after this instant runs."""


def test_a_rewrite_stopped_before_its_one_last_step_leaves_the_old_file_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new contents are written beside the file and put in its place in one step.

    Stopped just before that step, the file is byte for byte what it was, and
    a reader started afterwards reads every record it held.
    """
    path = tmp_path / "wal.jsonl"
    wal = _wal(path, tmp_path)
    wal.append_many([_Record(seq=seq, value=str(seq)) for seq in range(1, 7)])
    before = path.read_bytes()

    def stopped_here(_source: object, _destination: object) -> None:
        raise _Stopped

    with monkeypatch.context() as stopping:
        stopping.setattr(os, "replace", stopped_here)
        with pytest.raises(_Stopped):
            wal.replace_all([_Record(seq=2, value="2"), _Record(seq=6, value="6")])

    assert path.read_bytes() == before
    assert [record.seq for record in _wal(path, tmp_path).read_all()] == [1, 2, 3, 4, 5, 6]
