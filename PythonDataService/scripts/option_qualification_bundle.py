"""The evidence bundle of an option order qualification on a Paper account (#3102, PRD #3095 part D).

Given an account, a bot and a window, ``bundle`` writes one new folder holding
what the Clerk's books and the capture journal hold for that bot in that
window, and a one-page summary of it:

- ``comparisons.json``: each option entry, with the comparison the Clerk
  recorded beside Alpaca's option buying power and, for a record written with
  judgment switched off, what the same arithmetic says of it now;
- ``option_orders.json``: the bot's option orders, their legs and executions;
- ``journal/<family>/<day>.jsonl``: the journal's lines of the window, as
  written (the request's two times and an option order's body among them);
- ``summary.md``: what was compared, what was unknown and why, any discrepancy,
  and every piece that could not be read.
- ``manifest.json``: the same facts for a machine.

It only reads. The Clerk's store is opened read-only and query-only (it never
takes the execution lease), the journal's files are read, and nothing is
written but the new folder, which may not lie inside either. It contacts no
broker and needs no credential. A piece it cannot read is named in the
summary and in the manifest, never skipped, and the exit code says so.

``recordings`` takes a bundle's journal lines and writes them sanitised, with
the rules ``scripts/hitl_alpaca_capture.py`` applies to what it captures
(``app.broker.capture.sanitize``): the fixtures a recording becomes are
committed beside the documented examples, never in place of them.

A bundle holds the account number and the owner's order identifiers: it is
evidence to read, and is never committed. Only ``recordings`` output is.

Usage, from ``PythonDataService/`` or the lane container's ``/app``::

    python -m scripts.option_qualification_bundle bundle \\
        --artifacts-root /app/artifacts/alpaca_clerk --account-id PA... --bot <bot id> \\
        --from-ms 1760000000000 --to-ms 1760003600000 --out-dir <new folder>

    python -m scripts.option_qualification_bundle recordings \\
        --bundle-dir <that folder> --out-dir <new folder under tests/fixtures/alpaca/>

Exit: 0 when everything was read, 3 when the folder was written with pieces
unread (named), 2 when the command was refused and nothing was written.

Canonical implementation: the judgment is ``app.options.buying_power_check.rejudge``
and nothing is computed here but counts and one subtraction.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.money import dollars
from app.broker.alpaca.clerk.sqlite.budget_projection import query_only_snapshot
from app.broker.alpaca.clerk.sqlite.entry_buying_power import COMPARED_KIND, NotComparedReason
from app.broker.alpaca.clerk.sqlite.facts import EntryBuyingPowerComparedFacts
from app.broker.alpaca.clerk.sqlite.option_order_legs import MULTI_LEG_PARENT_SQL, NOT_A_LEG_SQL
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME
from app.broker.alpaca.clerk.sqlite.writes import account_paths
from app.broker.alpaca.config import BROKER_ID
from app.broker.capture.journal import CaptureEndpoint, CaptureSettings, utc_day
from app.broker.capture.sanitize import sanitize_recording
from app.options.buying_power_check import BuyingPowerCheckResult, rejudge
from app.services.option_buying_power_views import REASON_SENTENCES, RESULT_LABELS
from app.utils.atomic_file import atomic_write_bytes
from app.utils.timestamps import now_ms_utc, ny_datetime
from scripts._operator_cli import jsonable, timestamp_ms

MANIFEST_KIND = "option_qualification_bundle"
_MS_PER_DAY = 86_400_000
# The results a comparison can come to that are a difference to be classified (ADR 0076 decision 26):
# the broker took something other than the cost, or took more than the bot set aside.
_DISCREPANCIES = frozenset(
    {BuyingPowerCheckResult.OVER_RESERVED.value, BuyingPowerCheckResult.UNDER_RESERVED_UNEXPLAINED.value}
)
_ENTRY_ROLE = "ENTRY"

_OPTION_ORDERS_SQL = (
    "SELECT o.order_ref, o.role, o.client_order_id, o.broker_order_id, o.broker_state, o.submitted_at_ms, "
    "o.updated_at_ms, e.effect_operation_id, e.kind AS effect_kind, e.state AS effect_state, "
    "e.created_at_ms AS effect_created_at_ms, oc.symbol AS contract_symbol "
    "FROM orders o JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
    "LEFT JOIN order_contracts oc ON oc.order_ref = o.order_ref "
    f"WHERE e.strategy_instance_id = ? AND {NOT_A_LEG_SQL} AND (oc.order_ref IS NOT NULL OR {MULTI_LEG_PARENT_SQL}) "
    "AND COALESCE(o.submitted_at_ms, e.created_at_ms) >= ? AND COALESCE(o.submitted_at_ms, e.created_at_ms) < ? "
    "ORDER BY COALESCE(o.submitted_at_ms, e.created_at_ms), o.order_ref"
)
_LEGS_SQL = (
    "SELECT order_ref, leg_number, leg_id, symbol, side, intent, ratio, contracts FROM option_order_legs "
    "WHERE parent_order_ref = ? ORDER BY leg_number"
)
_FILLS_SQL = (
    "SELECT fill_id, order_ref, execution_id, side, qty, price, fee, fee_fidelity, is_correction, event_kind, "
    "evidence_source, source_event_at_ms, clerk_observed_at_ms FROM fills WHERE order_ref = ? "
    "ORDER BY recorded_transition_sequence"
)
_COMPARISON_SQL = (
    "SELECT sequence, clerk_observed_at_ms, facts_json FROM custody_transitions "
    "WHERE effect_operation_id = ? AND transition_kind = ? ORDER BY sequence LIMIT 1"
)
_MULTIPLIER_SQL = "SELECT multiplier FROM option_contracts WHERE symbol = ?"


class BundleRefused(ValueError):
    """The command cannot be carried out as given, and nothing was written."""


@dataclass(frozen=True, slots=True)
class Gap:
    """One piece the bundle could not read, and why."""

    piece: str
    why: str


@dataclass(frozen=True, slots=True)
class BundleResult:
    out_dir: Path
    not_read: tuple[Gap, ...]


@dataclass(frozen=True, slots=True)
class _Window:
    from_ms: int
    to_ms: int


# ── the books ────────────────────────────────────────────────────────────


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


@dataclass(frozen=True, slots=True)
class _Books:
    """What the Clerk's books gave for the bot, or why nothing can be said of its entries."""

    orders: list[dict[str, Any]]
    multipliers: dict[str, int]
    # Why the bot's entries were not assessed: the books were not read, or the bot is not on them, or its
    # orders could not be read. ``None`` when they were, and an empty ``orders`` then means it made none.
    not_assessed: str | None = None


def _read_books(account_dir: Path, bot: str, window: _Window, gaps: list[Gap]) -> _Books:
    """The bot's option orders with their legs, executions and comparison."""
    db_path = account_dir / DB_FILENAME
    if not db_path.is_file():
        gaps.append(Gap("the Clerk's books", f"there is no {DB_FILENAME} for this account at {db_path}"))
        return _Books([], {}, "the Clerk's books were not read, so nothing can be said of the bot's entries")
    try:
        with query_only_snapshot(db_path) as conn:
            return _orders_of(conn, bot, window, gaps)
    except sqlite3.Error as exc:
        gaps.append(Gap("the Clerk's books", f"{db_path} could not be read: {exc}"))
        return _Books([], {}, "the Clerk's books were not read, so nothing can be said of the bot's entries")


def _orders_of(conn: sqlite3.Connection, bot: str, window: _Window, gaps: list[Gap]) -> _Books:
    if conn.execute("SELECT 1 FROM strategy_instances WHERE strategy_instance_id = ?", (bot,)).fetchone() is None:
        gaps.append(Gap("the bot", f"{bot!r} is not a bot on this account's books"))
        return _Books([], {}, f"{bot!r} is not a bot on this account's books, so nothing can be said of its entries")
    try:
        orders = _rows(conn, _OPTION_ORDERS_SQL, (bot, window.from_ms, window.to_ms))
    except sqlite3.Error as exc:
        gaps.append(Gap("the bot's option orders", f"could not be read: {exc}"))
        return _Books([], {}, "the bot's option orders could not be read, so nothing can be said of its entries")
    multipliers: dict[str, int] = {}
    for order in orders:
        order_ref = order["order_ref"]
        order["legs"] = _piece(conn, gaps, f"the legs of {order_ref}", _LEGS_SQL, (order_ref,))
        refs = [order_ref, *(leg["order_ref"] for leg in order["legs"])]
        order["fills"] = [
            fill
            for ref in refs
            for fill in _piece(conn, gaps, f"the executions of {ref}", _FILLS_SQL, (ref,))
        ]
        symbols = {leg["symbol"] for leg in order["legs"]} | (
            {order["contract_symbol"]} if order["contract_symbol"] else set()
        )
        for symbol in sorted(symbols - multipliers.keys()):
            found = _piece(conn, gaps, f"the multiplier of {symbol}", _MULTIPLIER_SQL, (symbol,))
            if found:
                multipliers[symbol] = found[0]["multiplier"]
        order["comparison"] = (
            _comparison_of(conn, order, gaps) if order["role"] == _ENTRY_ROLE else None
        )
    return _Books(orders, multipliers)


def _piece(conn: sqlite3.Connection, gaps: list[Gap], piece: str, sql: str, params: tuple[Any, ...]) -> list[dict]:
    """Rows of one piece of the books; the piece is named in ``gaps`` and empty when it could not be read."""
    try:
        return _rows(conn, sql, params)
    except sqlite3.Error as exc:
        gaps.append(Gap(piece, f"could not be read: {exc}"))
        return []


def _comparison_of(conn: sqlite3.Connection, order: dict[str, Any], gaps: list[Gap]) -> dict[str, Any]:
    """An entry's recorded comparison as it was stored, and what offline judgment says of it."""
    piece = f"the comparison of {order['order_ref']}"
    recorded = _piece(conn, gaps, piece, _COMPARISON_SQL, (order["effect_operation_id"], COMPARED_KIND))
    if not recorded:
        return {"recorded": False, "stored": None, "offline_result": None, "offline_error": None, "unreadable": None}
    row = recorded[0]
    try:
        facts = EntryBuyingPowerComparedFacts.from_facts_json(row["facts_json"])
    except (TypeError, ValueError) as exc:
        gaps.append(Gap(piece, f"is on the books and could not be read: {exc}"))
        return {
            "recorded": True, "stored": None, "offline_result": None, "offline_error": None, "unreadable": str(exc),
        }
    offline_result: str | None = None
    offline_error: str | None = None
    try:
        judged = rejudge(facts)
        offline_result = None if judged is None else judged.value
    except ValueError as exc:
        offline_error = str(exc)
        gaps.append(Gap(f"the offline judgment of {order['order_ref']}", f"was not made: {exc}"))
    return {
        "recorded": True,
        "stored": {"sequence": row["sequence"], "observed_at_ms": row["clerk_observed_at_ms"], **asdict(facts)},
        "offline_result": offline_result,
        "offline_error": offline_error,
        "unreadable": None,
    }


# ── the journal ──────────────────────────────────────────────────────────


def _days(window: _Window) -> list[str]:
    """Every UTC day file name the window touches, first to last."""
    first = window.from_ms - window.from_ms % _MS_PER_DAY
    return [utc_day(day_ms) for day_ms in range(first, window.to_ms, _MS_PER_DAY)]


def _in_window(line: dict[str, Any], window: _Window) -> bool | None:
    """Whether the exchange a line records overlaps the window; ``None`` when the line says no time."""
    captured = line.get("captured_at_ms")
    if isinstance(captured, bool) or not isinstance(captured, int):
        return None
    began, ended = line.get("began_at_ms", captured), line.get("ended_at_ms", captured)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (began, ended)):
        began = ended = captured
    return began < window.to_ms and ended >= window.from_ms


def _read_journal(
    capture_dir: Path, broker: str, window: _Window, gaps: list[Gap]
) -> tuple[dict[tuple[str, str], list[str]], list[str]]:
    """The journal's lines of the window by (family, day), as written, and the day files that are not there."""
    lines: dict[tuple[str, str], list[str]] = {}
    absent: list[str] = []
    if not (capture_dir / broker).is_dir():
        gaps.append(Gap("the capture journal", f"there is no {broker} journal under {capture_dir}"))
        return lines, absent
    for family in CaptureEndpoint:
        for day in _days(window):
            path = capture_dir / broker / family.value / f"{day}.jsonl"
            if not path.is_file():
                absent.append(f"{family.value}/{day}.jsonl")
                continue
            kept = _lines_of(path, window, f"{family.value}/{day}.jsonl", gaps)
            if kept:
                lines[(family.value, day)] = kept
    return lines, absent


def _lines_of(path: Path, window: _Window, piece: str, gaps: list[Gap]) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        gaps.append(Gap(f"journal {piece}", f"could not be read: {exc}"))
        return []
    kept: list[str] = []
    for number, text_line in enumerate(text.splitlines(), start=1):
        if not text_line.strip():
            continue
        try:
            line = json.loads(text_line)
        except ValueError:
            line = None
        inside = _in_window(line, window) if isinstance(line, dict) else None
        if inside is None:
            gaps.append(Gap(f"journal {piece} line {number}", "is not a journal line this bundle can place in time"))
        elif inside:
            kept.append(text_line)
    return kept


# ── the summary ──────────────────────────────────────────────────────────


def _et(ms: int | None) -> str:
    return "—" if ms is None else f"{ms} ({ny_datetime(ms):%Y-%m-%d %H:%M:%S} ET)"


def _money(cents: int | None) -> str:
    return "—" if cents is None else f"${dollars(cents)}"


def _how_it_came_to(comparison: dict[str, Any]) -> tuple[str | None, str, NotComparedReason | None]:
    """What an entry's comparison comes to, who judged it, and why not when nobody did.

    The judge is ``"by the Clerk"`` for a result the Clerk judged,
    ``"offline"`` for a record written with judgment off and judged now, and
    ``""`` for an entry nobody has judged, which then has its reason.
    """
    if not comparison["recorded"]:
        return None, "", NotComparedReason.NOT_COMPARED_YET
    stored = comparison["stored"]
    if stored is None:
        return None, "", NotComparedReason.COMPARISON_FAILED
    if stored["result"] != BuyingPowerCheckResult.UNKNOWN.value:
        return stored["result"], "by the Clerk", None
    offline = comparison["offline_result"]
    # An offline "unknown" is not a judgment: the entry stays as unknown as the Clerk recorded it.
    if offline is not None and offline != BuyingPowerCheckResult.UNKNOWN.value:
        return offline, "offline", None
    try:
        return None, "", NotComparedReason(stored["reason_code"])
    except ValueError:
        return None, "", NotComparedReason.COMPARISON_FAILED


def _entry_line(order: dict[str, Any]) -> str:
    comparison = order["comparison"]
    stored = comparison["stored"]
    contract = order["contract_symbol"] or ", ".join(leg["symbol"] for leg in order["legs"])
    result, judge, reason = _how_it_came_to(comparison)
    if stored is None:
        return f"| {order['order_ref']} | {contract} | {reason.value if reason else ''} | — | — | — | — |"
    clerk = stored["result"] if stored["reason_code"] is None else f"unknown ({stored['reason_code']})"
    judged = f"{result} ({judge})" if result is not None else f"unknown ({reason.value if reason else ''})"
    before, after = stored["account_requirement_before_cents"], stored["account_requirement_after_cents"]
    change = None if before is None or after is None else after - before
    return (
        f"| {order['order_ref']} | {contract} | {clerk} | {judged} | "
        f"{_money(stored['commitment_cents'])} | {_money(change)} | {_money(stored['fall_cents'])} |"
    )


def _summary(
    *, account_id: str, bot: str, window: _Window, made_at_ms: int, books: _Books,
    journal: dict[tuple[str, str], list[str]], absent: list[str], gaps: list[Gap],
) -> str:
    orders = books.orders
    # A section that cannot be assessed says so and why. It never reads as "nothing" or "none".
    not_assessed = None if books.not_assessed is None else f"Not assessed: {books.not_assessed} (see Not read)."
    entries = [order for order in orders if order["role"] == _ENTRY_ROLE]
    judged_by_clerk: Counter[str] = Counter()
    judged_offline: Counter[str] = Counter()
    unknown: Counter[NotComparedReason] = Counter()
    for entry in entries:
        result, judge, reason = _how_it_came_to(entry["comparison"])
        if result is None and reason is not None:
            unknown[reason] += 1
        elif result is not None and judge == "by the Clerk":
            judged_by_clerk[result] += 1
        elif result is not None:
            judged_offline[result] += 1
    parts = [
        "# Option order qualification bundle",
        "",
        "Not for commit: this folder holds the account number and the owner's order identifiers. "
        "Fixtures are made from it with `recordings`.",
        "",
        f"- Account `{account_id}`, bot `{bot}`",
        f"- Window, int64 ms UTC, from {_et(window.from_ms)} up to {_et(window.to_ms)}",
        f"- Made {_et(made_at_ms)}",
        "",
        "## What was compared",
        "",
    ]
    if not_assessed is not None:
        parts.append(not_assessed)
    else:
        parts += [
            f"- Option orders of the bot in the window: {len(orders)}; entries among them: {len(entries)}.",
            f"- Entries judged by the Clerk (judgment was on): {sum(judged_by_clerk.values())}"
            + _breakdown(judged_by_clerk),
            f"- Entries the Clerk recorded with judgment off, judged offline now: {sum(judged_offline.values())}"
            + _breakdown(judged_offline),
            f"- Entries nobody has judged: {sum(unknown.values())}",
        ]
    parts += ["", "## What was unknown and why", ""]
    if not_assessed is not None:
        parts.append(not_assessed)
    elif not unknown:
        parts.append("Nothing was unknown.")
    for reason, count in sorted(unknown.items(), key=lambda item: (-item[1], item[0].value)):
        parts.append(f"- {count} x `{reason.value}`: {REASON_SENTENCES[reason]}")
    parts += ["", "## Entries", ""]
    if not_assessed is not None:
        parts.append(not_assessed)
    elif entries:
        parts += [
            "| Entry | Contract | The Clerk recorded | Judged | Commitment | "
            "Account requirement change | Alpaca's fall |",
            "|---|---|---|---|---|---|---|",
            *(_entry_line(entry) for entry in entries),
        ]
    else:
        parts.append("No option entry in the window.")
    parts += ["", "## Discrepancy", ""]
    found = []
    for entry in entries:
        result, judge, _ = _how_it_came_to(entry["comparison"])
        if result in _DISCREPANCIES:
            found.append((entry, BuyingPowerCheckResult(result), judge))
    if not_assessed is not None:
        parts.append(not_assessed)
    elif not found:
        parts.append("None. No judged entry differs from what it cost, and nothing here says one does.")
    for entry, result, judge in found:
        stored = entry["comparison"]["stored"]
        parts.append(
            f"- `{entry['order_ref']}`: **{RESULT_LABELS[result]}** (judged {judge}). Alpaca's option buying "
            f"power fell {_money(stored['fall_cents'])}; what filled cost "
            f"{_money(stored['cost_of_filled_cents'])} (premium paid {_money(stored['premium_paid_cents'])}, plus "
            f"the requirement for it); the bot set aside {_money(stored['set_aside_for_filled_cents'])} for it and "
            f"{_money(stored['commitment_cents'])} for the whole entry. Classify it by its cause before anything "
            "is changed."
        )
    parts += ["", "## The journal in the window", ""]
    if not journal:
        parts.append("No journal line fell in the window.")
    for (family, day), lines in sorted(journal.items()):
        parts.append(f"- `{family}/{day}.jsonl`: {len(lines)} lines")
    if absent:
        parts.append(f"- No file for: {', '.join(f'`{name}`' for name in absent)}")
    parts += ["", "## Not read", ""]
    if not gaps:
        parts.append("Nothing was left unread.")
    parts += [f"- {gap.piece}: {gap.why}" for gap in gaps]
    return "\n".join(parts) + "\n"


def _breakdown(counts: Counter[str]) -> str:
    return "" if not counts else " (" + ", ".join(f"{count} {result}" for result, count in sorted(counts.items())) + ")"


# ── the bundle ───────────────────────────────────────────────────────────


def _new_folder(out_dir: Path, *, reading: tuple[Path, ...]) -> Path:
    """``out_dir`` resolved, when it is new or empty and lies inside nothing this command reads."""
    resolved = out_dir.resolve()
    for root in reading:
        root = root.resolve()
        if resolved == root or root in resolved.parents:
            raise BundleRefused(f"{out_dir} lies inside {root}, which this command only reads")
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise BundleRefused(f"{out_dir} exists and is not empty; a bundle is written to a new folder")
    return resolved


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(jsonable(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def write_bundle(
    *,
    artifacts_root: Path,
    account_id: str,
    bot: str,
    from_ms: int,
    to_ms: int,
    capture_dir: Path,
    out_dir: Path,
    broker: str = BROKER_ID,
    made_at_ms: int | None = None,
) -> BundleResult:
    """Write the bundle of one bot's option orders in ``[from_ms, to_ms)`` into the new folder ``out_dir``."""
    if not from_ms < to_ms:
        raise BundleRefused(f"the window is empty: from {from_ms} is not before to {to_ms}")
    try:
        _, account_dir = account_paths(artifacts_root, account_id)
    except ValueError as exc:
        raise BundleRefused(str(exc)) from exc
    folder = _new_folder(out_dir, reading=(account_dir, capture_dir))
    window = _Window(from_ms, to_ms)
    made = now_ms_utc() if made_at_ms is None else made_at_ms
    gaps: list[Gap] = []

    books = _read_books(account_dir, bot, window, gaps)
    journal, absent = _read_journal(capture_dir, broker, window, gaps)

    comparisons = [
        {
            "order_ref": order["order_ref"],
            "effect_operation_id": order["effect_operation_id"],
            "contract": order["contract_symbol"] or [leg["symbol"] for leg in order["legs"]],
            **order["comparison"],
        }
        for order in books.orders
        if order["comparison"] is not None
    ]
    # The comparison is ``comparisons.json``'s; an order's own file does not hold it twice.
    option_orders = (
        None
        if books.not_assessed is not None
        else [{key: value for key, value in order.items() if key != "comparison"} for order in books.orders]
    )
    for (family, day), lines in journal.items():
        atomic_write_bytes(folder / "journal" / family / f"{day}.jsonl", ("\n".join(lines) + "\n").encode("utf-8"), mode=0o600)
    atomic_write_bytes(folder / "comparisons.json", _json_bytes(comparisons), mode=0o600)
    atomic_write_bytes(
        folder / "option_orders.json",
        _json_bytes(
            {"orders": option_orders, "multipliers": books.multipliers, "read": books.not_assessed is None}
        ),
        mode=0o600,
    )
    atomic_write_bytes(
        folder / "summary.md",
        _summary(
            account_id=account_id, bot=bot, window=window, made_at_ms=made, books=books, journal=journal,
            absent=absent, gaps=gaps,
        ).encode("utf-8"),
        mode=0o600,
    )
    atomic_write_bytes(
        folder / "manifest.json",
        _json_bytes(
            {
                "kind": MANIFEST_KIND,
                "version": 1,
                "account_id": account_id,
                "bot": bot,
                "broker": broker,
                "from_ms": from_ms,
                "to_ms": to_ms,
                "made_at_ms": made,
                "journal_lines": {f"{family}/{day}": len(lines) for (family, day), lines in sorted(journal.items())},
                "journal_files_absent": absent,
                "not_assessed": books.not_assessed,
                "not_read": [asdict(gap) for gap in gaps],
            }
        ),
        mode=0o600,
    )
    return BundleResult(out_dir=folder, not_read=tuple(gaps))


# ── recordings ───────────────────────────────────────────────────────────


def write_recordings(*, bundle_dir: Path, out_dir: Path) -> BundleResult:
    """Write a bundle's journal lines, sanitised, as recordings into the new folder ``out_dir``.

    A line that cannot be sanitised is left out and named. Nothing is
    written when a sanitised line still carries the bundle's account id: it
    would be committed.
    """
    try:
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleRefused(f"{bundle_dir} is not a bundle: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("kind") != MANIFEST_KIND:
        raise BundleRefused(f"{bundle_dir} is not an option qualification bundle")
    account_id = manifest.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        raise BundleRefused(f"{bundle_dir}'s manifest names no account")
    folder = _new_folder(out_dir, reading=(bundle_dir,))

    gaps: list[Gap] = []
    uuid_map: dict[str, str] = {}
    written: dict[str, list[str]] = {}
    journal_dir = bundle_dir / "journal"
    if not journal_dir.is_dir():
        gaps.append(Gap("the bundle's journal", "the bundle holds no journal folder, so there is nothing to record"))
    for family_dir in sorted(path for path in journal_dir.glob("*") if path.is_dir()):
        for day_file in sorted(family_dir.glob("*.jsonl")):
            for number, text in enumerate(day_file.read_text(encoding="utf-8").splitlines(), start=1):
                if not text.strip():
                    continue
                piece = f"{family_dir.name}/{day_file.name} line {number}"
                try:
                    recording = sanitize_recording(json.loads(text), uuid_map=uuid_map, account_ids=(account_id,))
                except (ValueError, TypeError) as exc:
                    gaps.append(Gap(piece, f"was left out: {exc}"))
                    continue
                encoded = json.dumps(recording, ensure_ascii=False)
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(account_id)}(?![A-Za-z0-9])", encoded):
                    raise BundleRefused(f"{piece} still carries the account id after sanitising; nothing was written")
                written.setdefault(family_dir.name, []).append(encoded)
    for family, lines in written.items():
        atomic_write_bytes(folder / f"{family}.jsonl", ("\n".join(lines) + "\n").encode("utf-8"))
    atomic_write_bytes(
        folder / "attribution.md",
        _attribution(manifest, {family: len(lines) for family, lines in written.items()}, gaps).encode("utf-8"),
    )
    return BundleResult(out_dir=folder, not_read=tuple(gaps))


def _attribution(manifest: dict[str, Any], counts: dict[str, int], gaps: list[Gap]) -> str:
    left_out = [f"- {gap.piece}: {gap.why}" for gap in gaps] or ["- None."]
    return "\n".join(
        [
            "# Recordings attribution: option orders on Alpaca Paper",
            "",
            f"- **broker:** {manifest.get('broker')} (paper)",
            f"- **window:** {manifest.get('from_ms')} up to {manifest.get('to_ms')} ms UTC",
            "- **source:** the capture journal of a live Alpaca Paper account, taken by "
            "`scripts/option_qualification_bundle.py bundle` and sanitised by `recordings`",
            "- **sanitization:** `app.broker.capture.sanitize`, the rules `scripts/hitl_alpaca_capture.py` applies: "
            "UUIDs replaced with sentinel values, the account number and the account's id replaced with "
            "`PA0SANITIZED00001`, client order ids and order refs replaced with one fixture token, secret-like "
            "keys redacted, authorization acknowledgements replaced with a structural placeholder. Times, "
            "statuses, symbols and numbers are as recorded.",
            "- **account:** not stated here by design",
            "",
            "## Lines by family",
            "",
            *(f"- `{family}.jsonl`: {count}" for family, count in sorted(counts.items())),
            "",
            "## Left out",
            "",
            *left_out,
            "",
            "Held beside the documented examples in `tests/fixtures/alpaca_documented/`, never in place of them.",
            "",
        ]
    )


# ── command line ─────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.option_qualification_bundle",
        description=(
            "Offline evidence for an option order qualification on a Paper account. Reads only; contacts no broker."
        ),
    )
    operations = parser.add_subparsers(dest="operation", required=True)

    bundle = operations.add_parser("bundle", help="Write the bundle of one bot's option orders in a window.")
    bundle.add_argument("--artifacts-root", type=Path, required=True, help="The lane's ALPACA_CLERK_DIR.")
    bundle.add_argument("--account-id", required=True)
    bundle.add_argument("--bot", required=True, help="The bot's strategy instance id.")
    bundle.add_argument("--from-ms", type=timestamp_ms, required=True, help="Window start, int64 ms UTC, inclusive.")
    bundle.add_argument("--to-ms", type=timestamp_ms, required=True, help="Window end, int64 ms UTC, exclusive.")
    bundle.add_argument(
        "--capture-dir", type=Path, default=None, help="The capture journal's root; BROKER_CAPTURE_DIR when omitted."
    )
    bundle.add_argument("--out-dir", type=Path, required=True, help="A new or empty folder.")

    recordings = operations.add_parser("recordings", help="Write a bundle's journal lines, sanitised, as recordings.")
    recordings.add_argument("--bundle-dir", type=Path, required=True)
    recordings.add_argument("--out-dir", type=Path, required=True, help="A new or empty folder.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.operation == "bundle":
            result = write_bundle(
                artifacts_root=args.artifacts_root,
                account_id=args.account_id,
                bot=args.bot,
                from_ms=args.from_ms,
                to_ms=args.to_ms,
                capture_dir=args.capture_dir if args.capture_dir is not None else CaptureSettings().dir,
                out_dir=args.out_dir,
            )
        else:
            result = write_recordings(bundle_dir=args.bundle_dir, out_dir=args.out_dir)
    except (BundleRefused, OSError) as exc:
        sys.stderr.write(f"Option qualification bundle refused: {exc}\n")
        return 2
    sys.stdout.write(f"Written to {result.out_dir}\n")
    for gap in result.not_read:
        sys.stdout.write(f"Not read: {gap.piece}: {gap.why}\n")
    return 3 if result.not_read else 0


if __name__ == "__main__":
    sys.exit(main())
