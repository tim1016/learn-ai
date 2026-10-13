"""The qualification kit's evidence bundle, run against the real books of a stand-in Paper account (#3102).

The Clerk here is the real SQLite Clerk of a Paper account over the resting
venue the comparison's own tests use (``test_entry_buying_power``): nothing
was sent to Alpaca or recorded from it. The bundle reads the books and a
journal this module writes, and any attempt at a network connection fails the
test.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.facts import EntryBuyingPowerComparedFacts
from app.broker.capture.journal import CaptureEndpoint, CaptureJournal, utc_day
from app.broker.capture.sanitize import ACCOUNT_NUMBER_SENTINEL, UUID_SENTINEL_BASE
from scripts import option_qualification_bundle as kit
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import SYMBOL
from tests.broker.alpaca.clerk.sqlite.test_entry_buying_power import (  # noqa: F401 -- pytest fixtures, used by name
    Paper,
    judged,
)
from tests.broker.alpaca.clerk.sqlite.test_entry_buying_power import paper as comparison_tests_paper  # noqa: F401

_HOUR = 3_600_000
_BOT = "a"


@pytest.fixture(name="paper")
def _paper(comparison_tests_paper: Paper) -> Paper:  # noqa: F811 -- the imported fixture, by its name
    """The comparison tests' Paper account: the real Clerk of a stand-in account, nothing sent to Alpaca."""
    return comparison_tests_paper


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kit contacts no broker: a connection attempt of any kind fails the test."""

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("the qualification kit opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _journal_line(
    root: Path, at_ms: int, endpoint: CaptureEndpoint = CaptureEndpoint.ORDERS, *, body: bytes = b"{}", **extra: Any
) -> str:
    """Write one journal line at ``at_ms`` through the real journal; the text of the line as it was written."""
    CaptureJournal(capture_dir=root, clock=lambda: at_ms).record(
        broker="alpaca", endpoint=endpoint, method="post", params={}, status=200, raw_body=body, **extra
    )
    path = root / "alpaca" / endpoint.value / f"{utc_day(at_ms)}.jsonl"
    return path.read_text(encoding="utf-8").splitlines()[-1]


def _write(paper: Paper, tmp_path: Path, **changes: Any) -> kit.BundleResult:
    arguments: dict[str, Any] = {
        "artifacts_root": paper.artifacts_root,
        "account_id": paper.repo.account_id,
        "bot": _BOT,
        "from_ms": NOON - _HOUR,
        "to_ms": paper.clock() + _HOUR,
        "capture_dir": tmp_path / "captures",
        "out_dir": tmp_path / "bundle",
        "made_at_ms": paper.clock(),
    }
    return kit.write_bundle(**(arguments | changes))


def _json(folder: Path, name: str) -> Any:
    return json.loads((folder / name).read_text(encoding="utf-8"))


def _section(summary: str, heading: str) -> str:
    """The text of one ``## heading`` section of a summary, up to the next."""
    _, _, rest = summary.partition(f"## {heading}\n")
    return rest.split("\n## ", 1)[0].strip()


def _assert_nothing_is_said_of_the_entries(summary: str, why: str) -> None:
    """Every section about the bot's entries says it was not assessed, and why; none reads as nothing or none."""
    for heading in ("What was compared", "What was unknown and why", "Entries", "Discrepancy"):
        section = _section(summary, heading)
        assert section.startswith("Not assessed: ") and why in section, f"{heading}: {section!r}"
    assert "Nothing was unknown." not in summary
    assert "None. No judged entry differs" not in summary


def _fingerprints(*roots: Path) -> dict[str, str]:
    """Every file under the roots by path and content hash, but for SQLite's shared-memory index, which a reader moves."""
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith("-shm")
    }


# ── offline judgment, on the Clerk's own record ──────────────────────────


@pytest.mark.parametrize(
    ("shows", "expected"),
    [
        # Alpaca's figure fell exactly the $194.00 paid, a cent more, and $250.00: the three results the Clerk
        # records when its setting is on (test_entry_buying_power), here judged from the record it wrote when off.
        ("99806.00", "consistent"),
        ("99805.99", "over_reserved"),
        ("99750.00", "under_reserved_unexplained"),
    ],
)
async def test_a_pair_recorded_with_judgment_off_is_judged_offline_to_what_the_clerk_would_have_said(
    paper: Paper, tmp_path: Path, shows: str, expected: str
) -> None:
    order_ref = await paper.filled(shows=shows)
    found = await paper.compared(order_ref)
    assert (found.result, found.reason_code) == ("unknown", "JUDGMENT_NOT_SWITCHED_ON")
    _journal_line(tmp_path / "captures", NOON + 1_000)

    result = _write(paper, tmp_path)

    [comparison] = _json(result.out_dir, "comparisons.json")
    assert comparison["order_ref"] == order_ref
    assert comparison["stored"]["result"] == "unknown"
    assert comparison["offline_result"] == expected
    assert comparison["offline_error"] is None
    assert result.not_read == ()
    # The Clerk's books are the books' own: nothing offline was written to them.
    assert paper.comparison(order_ref) == found


async def test_the_bundle_holds_the_comparison_the_orders_the_journal_of_the_window_and_a_summary(
    paper: Paper, tmp_path: Path
) -> None:
    order_ref = await paper.filled(shows="99750.00")
    await paper.compared(order_ref)
    captures = tmp_path / "captures"
    inside = _journal_line(
        captures, NOON + 1_000, body=b'{"status":"accepted"}', began_at_ms=NOON + 700, ended_at_ms=NOON + 950,
        request_body=b'{"symbol":"' + SYMBOL.encode() + b'","qty":"1"}',
    )
    _journal_line(captures, NOON - 5 * _HOUR, body=b'{"status":"before the window"}')
    listed = _journal_line(captures, NOON, CaptureEndpoint.OPTION_CONTRACTS, body=b'{"tradable":true}')

    result = _write(paper, tmp_path)

    folder = result.out_dir
    day = utc_day(NOON + 1_000)
    assert (folder / "journal" / "orders" / f"{day}.jsonl").read_text().splitlines() == [inside]
    assert (folder / "journal" / "option_contracts" / f"{day}.jsonl").read_text().splitlines() == [listed]
    orders = _json(folder, "option_orders.json")
    [entry] = orders["orders"]
    assert (entry["order_ref"], entry["role"], entry["contract_symbol"]) == (order_ref, "ENTRY", SYMBOL)
    assert [(fill["side"], fill["qty"], fill["price"]) for fill in entry["fills"]] == [("BUY", 1.0, 1.94)]
    assert orders["multipliers"] == {SYMBOL: 100}
    manifest = _json(folder, "manifest.json")
    assert (manifest["account_id"], manifest["bot"], manifest["from_ms"]) == (paper.repo.account_id, _BOT, NOON - _HOUR)
    assert manifest["not_read"] == []
    assert manifest["journal_lines"] == {f"option_contracts/{day}": 1, f"orders/{day}": 1}
    summary = (folder / "summary.md").read_text()
    assert "Entries the Clerk recorded with judgment off, judged offline now: 1 (1 under_reserved_unexplained)" in summary
    assert "Under-reserved" in summary and order_ref in summary
    assert "Nothing was left unread." in summary
    assert "$250.00" in summary and "$194.00" in summary


async def test_an_entry_the_clerk_has_not_compared_yet_is_listed_as_that_and_never_as_agreement(
    paper: Paper, tmp_path: Path
) -> None:
    order_ref = await paper.filled(shows="99806.00")

    result = _write(paper, tmp_path)

    [comparison] = _json(result.out_dir, "comparisons.json")
    assert (comparison["order_ref"], comparison["recorded"], comparison["offline_result"]) == (order_ref, False, None)
    summary = (result.out_dir / "summary.md").read_text()
    assert "1 x `NOT_COMPARED_YET`" in summary
    assert "Entries nobody has judged: 1" in summary
    assert "consistent" not in summary.lower()


@pytest.mark.usefixtures("judged")
async def test_a_comparison_recorded_with_judgment_on_keeps_the_clerks_own_result(
    paper: Paper, tmp_path: Path
) -> None:
    order_ref = await paper.filled(shows="99750.00")
    await paper.compared(order_ref)

    result = _write(paper, tmp_path)

    [comparison] = _json(result.out_dir, "comparisons.json")
    assert comparison["stored"]["result"] == "under_reserved_unexplained"
    # Judged already: there is nothing for offline judgment to say.
    assert comparison["offline_result"] is None
    summary = (result.out_dir / "summary.md").read_text()
    assert "Entries judged by the Clerk (judgment was on): 1 (1 under_reserved_unexplained)" in summary
    assert "judged by the Clerk" in summary


# ── it only reads, and says what it could not ────────────────────────────


async def test_the_bundle_only_reads_the_books_and_the_journal(paper: Paper, tmp_path: Path) -> None:
    order_ref = await paper.filled(shows="99806.00")
    await paper.compared(order_ref)
    captures = tmp_path / "captures"
    _journal_line(captures, NOON + 1_000)
    reads = (paper.artifacts_root / "accounts", captures)
    before = _fingerprints(*reads)
    # Not vacuous: the books and the journal are among what was fingerprinted.
    assert any(name.endswith("clerk.db") for name in before)
    assert any(name.endswith(".jsonl") for name in before)

    _write(paper, tmp_path)
    kit.main(
        [
            "bundle", "--artifacts-root", str(paper.artifacts_root), "--account-id", paper.repo.account_id,
            "--bot", _BOT, "--from-ms", str(NOON - _HOUR), "--to-ms", str(paper.clock() + _HOUR),
            "--capture-dir", str(captures), "--out-dir", str(tmp_path / "second-bundle"),
        ]
    )

    assert _fingerprints(*reads) == before


async def test_a_piece_that_cannot_be_read_is_named_in_the_summary_and_the_rest_is_still_written(
    paper: Paper, tmp_path: Path
) -> None:
    captures = tmp_path / "captures"
    _journal_line(captures, NOON + 1_000)
    day = utc_day(NOON + 1_000)
    (captures / "alpaca" / "orders" / f"{day}.jsonl").write_text(
        (captures / "alpaca" / "orders" / f"{day}.jsonl").read_text() + "this is not a journal line\n"
    )

    result = _write(paper, tmp_path, artifacts_root=tmp_path / "no-such-lane")

    pieces = {gap.piece for gap in result.not_read}
    assert "the Clerk's books" in pieces
    assert f"journal orders/{day}.jsonl line 2" in pieces
    summary = (result.out_dir / "summary.md").read_text()
    assert "## Not read" in summary and "no clerk.db for this account" in summary
    assert "Nothing was left unread." not in summary
    _assert_nothing_is_said_of_the_entries(summary, "the Clerk's books were not read")
    # The journal's good line is there all the same.
    assert len((result.out_dir / "journal" / "orders" / f"{day}.jsonl").read_text().splitlines()) == 1
    assert _json(result.out_dir, "option_orders.json")["read"] is False


async def test_a_missing_journal_is_named_and_the_books_are_still_read(paper: Paper, tmp_path: Path) -> None:
    order_ref = await paper.filled(shows="99806.00")
    await paper.compared(order_ref)

    result = _write(paper, tmp_path, capture_dir=tmp_path / "no-journal-here")

    assert [gap.piece for gap in result.not_read] == ["the capture journal"]
    [comparison] = _json(result.out_dir, "comparisons.json")
    assert comparison["offline_result"] == "consistent"


async def test_a_bot_the_books_do_not_know_is_named_and_not_read_as_a_bot_with_no_orders(
    paper: Paper, tmp_path: Path
) -> None:
    result = _write(paper, tmp_path, bot="no-such-bot")

    assert "the bot" in {gap.piece for gap in result.not_read}
    summary = (result.out_dir / "summary.md").read_text()
    assert "not a bot on this account's books" in summary
    _assert_nothing_is_said_of_the_entries(summary, "'no-such-bot' is not a bot on this account's books")
    manifest = _json(result.out_dir, "manifest.json")
    assert manifest["not_assessed"] is not None
    assert _json(result.out_dir, "option_orders.json")["read"] is False


async def test_a_bot_that_made_no_option_order_in_the_window_is_assessed_and_says_none_and_nothing(
    paper: Paper, tmp_path: Path
) -> None:
    _journal_line(tmp_path / "captures", NOON + 1_000)

    result = _write(paper, tmp_path)

    summary = (result.out_dir / "summary.md").read_text()
    assert result.not_read == ()
    assert "Not assessed" not in summary
    assert "Nothing was unknown." in _section(summary, "What was unknown and why")
    assert _section(summary, "Discrepancy").startswith("None.")
    assert _json(result.out_dir, "manifest.json")["not_assessed"] is None
    assert _json(result.out_dir, "option_orders.json") == {"multipliers": {}, "orders": [], "read": True}


async def test_a_record_the_books_hold_and_this_build_cannot_read_is_named_not_dropped(
    paper: Paper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_ref = await paper.filled(shows="99806.00")
    await paper.compared(order_ref)
    _journal_line(tmp_path / "captures", NOON + 1_000)

    def unreadable(cls: type, facts_json: str) -> None:
        raise ValueError("a field this build does not know")

    monkeypatch.setattr(EntryBuyingPowerComparedFacts, "from_facts_json", classmethod(unreadable))

    result = _write(paper, tmp_path)

    [comparison] = _json(result.out_dir, "comparisons.json")
    assert (comparison["recorded"], comparison["stored"]) == (True, None)
    assert comparison["unreadable"] == "a field this build does not know"
    assert [gap.piece for gap in result.not_read] == [f"the comparison of {order_ref}"]
    assert "1 x `COMPARISON_FAILED`" in (result.out_dir / "summary.md").read_text()


# ── what it will not do ──────────────────────────────────────────────────


async def test_the_bundle_is_refused_inside_the_stores_it_reads_and_into_a_folder_that_is_not_empty(
    paper: Paper, tmp_path: Path
) -> None:
    inside_books = paper.repo.db_path.parent / "bundle"
    with pytest.raises(kit.BundleRefused, match="only reads"):
        _write(paper, tmp_path, out_dir=inside_books)
    inside_journal = tmp_path / "captures" / "bundle"
    with pytest.raises(kit.BundleRefused, match="only reads"):
        _write(paper, tmp_path, out_dir=inside_journal)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "earlier.txt").write_text("an earlier bundle")
    with pytest.raises(kit.BundleRefused, match="not empty"):
        _write(paper, tmp_path, out_dir=occupied)
    assert not inside_books.exists() and not inside_journal.exists()


async def test_an_empty_window_is_refused(paper: Paper, tmp_path: Path) -> None:
    with pytest.raises(kit.BundleRefused, match="window is empty"):
        _write(paper, tmp_path, from_ms=NOON, to_ms=NOON)


async def test_the_command_exits_zero_when_whole_three_when_a_piece_is_unread_and_two_when_refused(
    paper: Paper, tmp_path: Path
) -> None:
    order_ref = await paper.filled(shows="99806.00")
    await paper.compared(order_ref)
    _journal_line(tmp_path / "captures", NOON + 1_000)
    common = [
        "--artifacts-root", str(paper.artifacts_root), "--account-id", paper.repo.account_id, "--bot", _BOT,
        "--from-ms", str(NOON - _HOUR), "--to-ms", str(paper.clock() + _HOUR),
    ]

    whole = kit.main(["bundle", *common, "--capture-dir", str(tmp_path / "captures"), "--out-dir", str(tmp_path / "a")])
    unread = kit.main(["bundle", *common, "--capture-dir", str(tmp_path / "none"), "--out-dir", str(tmp_path / "b")])
    refused = kit.main(["bundle", *common, "--capture-dir", str(tmp_path / "captures"), "--out-dir", str(tmp_path / "a")])

    assert (whole, unread, refused) == (0, 3, 2)


# ── recordings: the journal's lines, sanitised ───────────────────────────


async def test_recordings_carry_no_account_number_and_no_key_and_keep_the_times(
    paper: Paper, tmp_path: Path
) -> None:
    account = paper.repo.account_id
    order_id = "61e69015-8549-4bfd-b9c3-01e75843f47d"
    captures = tmp_path / "captures"
    body = json.dumps({"id": order_id, "account_number": account, "client_order_id": "alpaca/a/entry-1", "status": "new"})
    _journal_line(
        captures, NOON + 1_000, body=body.encode(), began_at_ms=NOON + 700, ended_at_ms=NOON + 950,
        request_body=json.dumps({"symbol": SYMBOL, "client_order_id": "alpaca/a/entry-1", "api_key": "PKTHEKEY"}).encode(),
    )
    stream = json.dumps({"stream": "authorization", "data": {"status": "authorized", "key_id": "PKTHEKEY"}})
    _journal_line(captures, NOON + 2_000, CaptureEndpoint.STREAM, body=stream.encode())
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle").out_dir

    recorded = kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "recordings")

    assert recorded.not_read == ()
    written = {path.name: path.read_text() for path in (tmp_path / "recordings").iterdir()}
    assert set(written) == {"orders.jsonl", "stream.jsonl", "attribution.md"}
    everything = "\n".join(written.values())
    assert account not in everything
    assert "PKTHEKEY" not in everything
    assert "alpaca/a/entry-1" not in everything
    [line] = [json.loads(text) for text in written["orders.jsonl"].splitlines()]
    assert json.loads(line["raw_body"])["id"] == UUID_SENTINEL_BASE.format(1)
    assert json.loads(line["raw_body"])["account_number"] == ACCOUNT_NUMBER_SENTINEL
    assert (line["began_at_ms"], line["ended_at_ms"], line["status"]) == (NOON + 700, NOON + 950, 200)
    assert json.loads(line["request_body"])["symbol"] == SYMBOL
    [frame] = [json.loads(text) for text in written["stream.jsonl"].splitlines()]
    assert json.loads(frame["raw_body"]) == {"stream": "authorization", "data": {"status": "authorized", "action": "authenticate"}}
    assert "**account:** not stated here by design" in written["attribution.md"]


async def test_recordings_are_not_written_when_a_sanitised_line_still_carries_the_account(
    paper: Paper, tmp_path: Path
) -> None:
    captures = tmp_path / "captures"
    # The account id as a JSON key: sanitising scrubs values, so this line would be committed with it.
    _journal_line(captures, NOON + 1_000, body=json.dumps({paper.repo.account_id: {"cash": "1"}}).encode())
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle").out_dir

    with pytest.raises(kit.BundleRefused, match="still carries the account id"):
        kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "recordings")

    assert not (tmp_path / "recordings").exists()


async def test_a_line_that_cannot_be_sanitised_is_left_out_and_named(paper: Paper, tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    _journal_line(captures, NOON + 1_000, body=b'{"fine":true}')
    _journal_line(captures, NOON + 2_000, body=b"\xff\xfe\x00not text")
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle").out_dir

    recorded = kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "recordings")

    assert [gap.piece for gap in recorded.not_read] == [f"orders/{utc_day(NOON + 1_000)}.jsonl line 2"]
    assert len((tmp_path / "recordings" / "orders.jsonl").read_text().splitlines()) == 1


async def test_opaque_text_bodies_are_never_written_and_each_refusal_is_reported_and_exits_three(
    paper: Paper, tmp_path: Path
) -> None:
    captures = tmp_path / "captures"
    _journal_line(captures, NOON + 1_000, body=b'{"fine":true}')
    # Text with a key and a client order id that is not a UUID, a plain-text body, and a bare JSON string.
    _journal_line(captures, NOON + 2_000, body=b"api_key=PKTHEKEY&client_order_id=my-order-7")
    _journal_line(captures, NOON + 3_000, body=b"upstream connect error or disconnect/reset before headers")
    _journal_line(captures, NOON + 4_000, body=b'"api_key=PKTHEKEY"')
    _journal_line(captures, NOON + 5_000, body=b'{"also":"fine"}')
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle").out_dir
    day = utc_day(NOON + 1_000)

    recorded = kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "recordings")

    assert [gap.piece for gap in recorded.not_read] == [f"orders/{day}.jsonl line {n}" for n in (2, 3, 4)]
    assert all("was not sanitised, so it is left out" in gap.why for gap in recorded.not_read)
    written = {path.name: path.read_text() for path in (tmp_path / "recordings").iterdir()}
    # Only the two lines that are JSON are there, and nothing of the three that are not.
    assert [json.loads(text)["raw_body"] for text in written["orders.jsonl"].splitlines()] == [
        '{"fine": true}',
        '{"also": "fine"}',
    ]
    for text in written.values():
        for opaque in ("PKTHEKEY", "my-order-7", "upstream connect error"):
            assert opaque not in text
    attribution = written["attribution.md"]
    assert "## Not sanitised, and left out" in attribution
    assert all(f"orders/{day}.jsonl line {n}" in attribution for n in (2, 3, 4))
    exit_code = kit.main(["recordings", "--bundle-dir", str(bundle), "--out-dir", str(tmp_path / "second")])
    assert exit_code == 3


async def test_a_body_that_is_opaque_text_in_every_line_writes_no_recording_of_that_family(
    paper: Paper, tmp_path: Path
) -> None:
    _journal_line(tmp_path / "captures", NOON + 1_000, body=b"api_key=PKTHEKEY")
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle").out_dir

    recorded = kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "recordings")

    assert len(recorded.not_read) == 1
    assert sorted(path.name for path in (tmp_path / "recordings").iterdir()) == ["attribution.md"]


async def test_distinct_drops_a_poll_that_repeats_the_one_before_and_keeps_a_change_back(
    paper: Paper, tmp_path: Path
) -> None:
    captures = tmp_path / "captures"
    ask_a, ask_b = b'{"ask":"a"}', b'{"ask":"b"}'
    # One request polled four times: empty, empty, an order, empty again. The second empty is a repeat; the
    # third is a change and the fourth is a change back, so both stay. A different request with the same
    # answer is a different poll, not a repeat.
    for at, body, request in [
        (1_000, b"[]", ask_a),
        (2_000, b"[]", ask_a),
        (3_000, b'[{"id":"one"}]', ask_a),
        (4_000, b"[]", ask_a),
        (5_000, b"[]", ask_b),
    ]:
        _journal_line(captures, NOON + at, body=body, request_body=request)
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle").out_dir

    kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "all")
    kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "distinct", distinct=True)

    assert len((tmp_path / "all" / "orders.jsonl").read_text().splitlines()) == 5
    kept = [json.loads(text) for text in (tmp_path / "distinct" / "orders.jsonl").read_text().splitlines()]
    assert [(line["captured_at_ms"] - NOON, line["raw_body"]) for line in kept] == [
        (1_000, "[]"),
        (3_000, '[{"id": "one"}]'),
        (4_000, "[]"),
        (5_000, "[]"),
    ]
    section = (tmp_path / "distinct" / "attribution.md").read_text().split("## Repeated polls dropped\n", 1)[1]
    assert "- `orders.jsonl`: 1" in section.split("\n## ", 1)[0]
    assert "## Repeated polls dropped" not in (tmp_path / "all" / "attribution.md").read_text()


async def test_distinct_drops_a_page_through_that_repeats_the_last_and_keeps_the_one_that_changed(
    paper: Paper, tmp_path: Path
) -> None:
    captures = tmp_path / "captures"
    page_one = (b'{"page":1}', b'{"answer":1}')
    page_two = (b'{"page":2}', b'{"answer":2}')
    page_two_changed = (b'{"page":2}', b'{"answer":2,"new":true}')
    # The account's activities are read a page at a time: the same two requests, answered the same way each
    # pass, until the last pass has a new row on its second page.
    polls = [page_one, page_two] * 3 + [page_one, page_two_changed]
    for at, (request, body) in enumerate(polls, start=1):
        _journal_line(captures, NOON + at * 1_000, body=body, request_body=request)
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle").out_dir

    kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "distinct", distinct=True)

    kept = [json.loads(text) for text in (tmp_path / "distinct" / "orders.jsonl").read_text().splitlines()]
    assert [(line["captured_at_ms"] - NOON) // 1_000 for line in kept] == [1, 2, 7, 8]
    assert json.loads(kept[-1]["raw_body"]) == {"answer": 2, "new": True}


async def test_only_writes_the_named_families_and_says_which_it_left_out(paper: Paper, tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    _journal_line(captures, NOON + 1_000, body=b'{"fine":true}')
    _journal_line(captures, NOON + 2_000, CaptureEndpoint.STREAM, body=b'{"stream":"trade_updates"}')
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle").out_dir

    recorded = kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "recordings", only=("orders",))

    assert recorded.not_read == ()
    assert sorted(path.name for path in (tmp_path / "recordings").iterdir()) == ["attribution.md", "orders.jsonl"]
    attribution = (tmp_path / "recordings" / "attribution.md").read_text()
    section = attribution.split("## Families left out\n", 1)[1].split("\n## ", 1)[0]
    assert "- `stream`" in section and "not a gap in the journal" in section
    exit_code = kit.main(
        ["recordings", "--bundle-dir", str(bundle), "--out-dir", str(tmp_path / "second"), "--only", "orders", "--distinct"]
    )
    assert exit_code == 0


async def test_a_family_the_bundle_has_no_journal_of_is_refused_rather_than_written_as_nothing(
    paper: Paper, tmp_path: Path
) -> None:
    _journal_line(tmp_path / "captures", NOON + 1_000, body=b'{"fine":true}')
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle").out_dir

    with pytest.raises(kit.BundleRefused, match="has no stream"):
        kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "recordings", only=("orders", "stream"))

    assert not (tmp_path / "recordings").exists()


async def test_the_journal_gaps_of_the_source_bundle_are_carried_into_the_recordings_and_exit_three(
    paper: Paper, tmp_path: Path
) -> None:
    captures = tmp_path / "captures"
    day = utc_day(NOON + 1_000)
    _journal_line(captures, NOON + 1_000, body=b'{"fine":true}')
    day_file = captures / "alpaca" / "orders" / f"{day}.jsonl"
    day_file.write_text(day_file.read_text() + "this is not a journal line\n")
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle")
    assert [gap.piece for gap in bundle.not_read] == [f"journal orders/{day}.jsonl line 2"]

    recorded = kit.write_recordings(bundle_dir=bundle.out_dir, out_dir=tmp_path / "recordings")

    # Every line of the journal that was read is recorded; the source's own gap is not forgotten.
    assert [gap.piece for gap in recorded.not_read] == [f"journal orders/{day}.jsonl line 2"]
    attribution = (tmp_path / "recordings" / "attribution.md").read_text()
    section = attribution.split("## Journal evidence the source bundle could not read\n", 1)[1].split("\n## ", 1)[0]
    assert f"journal orders/{day}.jsonl line 2" in section
    assert "the source bundle could not read it" in section
    assert "- None." not in section
    exit_code = kit.main(["recordings", "--bundle-dir", str(bundle.out_dir), "--out-dir", str(tmp_path / "second")])
    assert exit_code == 3


async def test_a_gap_in_the_books_is_not_a_gap_in_the_recordings(paper: Paper, tmp_path: Path) -> None:
    _journal_line(tmp_path / "captures", NOON + 1_000, body=b'{"fine":true}')
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle", artifacts_root=tmp_path / "no-such-lane")
    assert [gap.piece for gap in bundle.not_read] == ["the Clerk's books"]

    recorded = kit.write_recordings(bundle_dir=bundle.out_dir, out_dir=tmp_path / "recordings")

    assert recorded.not_read == ()
    exit_code = kit.main(["recordings", "--bundle-dir", str(bundle.out_dir), "--out-dir", str(tmp_path / "second")])
    assert exit_code == 0


async def test_a_manifest_that_does_not_say_what_it_could_not_read_is_not_recorded(
    paper: Paper, tmp_path: Path
) -> None:
    _journal_line(tmp_path / "captures", NOON + 1_000, body=b'{"fine":true}')
    bundle = _write(paper, tmp_path, out_dir=tmp_path / "bundle").out_dir
    manifest = json.loads((bundle / "manifest.json").read_text())
    del manifest["not_read"]
    (bundle / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(kit.BundleRefused, match="does not say what it could not read"):
        kit.write_recordings(bundle_dir=bundle, out_dir=tmp_path / "recordings")

    assert not (tmp_path / "recordings").exists()


def test_an_offline_unknown_is_not_a_judgment_and_the_entry_stays_as_unknown_as_it_was_recorded() -> None:
    comparison = {
        "recorded": True,
        "stored": {"result": "unknown", "reason_code": "JUDGMENT_NOT_SWITCHED_ON"},
        "offline_result": "unknown",
    }

    result, judge, reason = kit._how_it_came_to(comparison)

    assert (result, judge) == (None, "")
    assert reason is not None and reason.value == "JUDGMENT_NOT_SWITCHED_ON"


def test_recordings_are_made_only_from_a_bundle(tmp_path: Path) -> None:
    (tmp_path / "not-a-bundle").mkdir()

    with pytest.raises(kit.BundleRefused, match="is not a bundle"):
        kit.write_recordings(bundle_dir=tmp_path / "not-a-bundle", out_dir=tmp_path / "recordings")
