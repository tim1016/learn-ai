"""Symbol-validity store + probe (#1795).

The durable fact behind retire's second eligibility proof: the sweep's
post-pass probe records definitive broker answers, and the read path consumes
them without broker I/O. The invariant under test throughout: only a
definitive answer (``get_asset`` returning an asset or ``None``) is ever
recorded -- an unreachable broker records nothing, so it can never mint a
false permanence fact.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.broker.alpaca.symbol_validity import (
    SymbolValidityObservation,
    SymbolValidityProbe,
    SymbolValidityStore,
    symbol_marked_unresolvable,
    symbol_unresolvable_for_mode,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerAsset


def _asset(symbol: str, *, tradable: bool = True, status: str = "active") -> BrokerAsset:
    return BrokerAsset(
        broker="alpaca",
        asset_id=f"asset-{symbol}",
        symbol=symbol,
        name=symbol,
        asset_class="us_equity",
        exchange="NASDAQ",
        status=status,
        tradable=tradable,
        fractionable=True,
        shortable=None,
        marginable=None,
    )


class _FakeRead:
    """get_asset-only read port: symbol -> asset, None, or a raised error."""

    def __init__(self, answers: dict[str, BrokerAsset | Exception | None]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        self.calls.append(symbol)
        answer = self.answers[symbol]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _probe(
    store: SymbolValidityStore,
    read: _FakeRead,
    symbols: list[str],
    *,
    now_ms: int = 1_000_000,
    **kwargs: object,
) -> SymbolValidityProbe:
    return SymbolValidityProbe(
        store=store,
        read=read,  # type: ignore[arg-type] -- duck-typed get_asset-only fake
        roster_symbols=lambda: symbols,
        now_ms=lambda: now_ms,
        min_interval_ms=0,
        **kwargs,  # type: ignore[arg-type]
    )


def test_store_round_trips_and_uppercases_symbols(tmp_path: Path) -> None:
    store = SymbolValidityStore(tmp_path)
    store.record(
        [SymbolValidityObservation(symbol="appl", observed_at_ms=5, resolvable=False)]
    )

    observation = store.read("APPL")
    assert observation is not None
    assert observation.resolvable is False
    assert store.read("appl") == observation
    assert store.path == tmp_path / "accounts" / "alpaca" / "symbol_validity.json"


def test_store_merge_preserves_other_symbols(tmp_path: Path) -> None:
    store = SymbolValidityStore(tmp_path)
    store.record([SymbolValidityObservation(symbol="APPL", observed_at_ms=5, resolvable=False)])
    store.record(
        [SymbolValidityObservation(symbol="SPY", observed_at_ms=6, resolvable=True, tradable=True)]
    )

    assert set(store.read_all()) == {"APPL", "SPY"}


def test_store_reads_missing_and_corrupt_files_as_empty(tmp_path: Path) -> None:
    store = SymbolValidityStore(tmp_path)
    assert store.read_all() == {}

    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.read_all() == {}

    store.path.write_text('["valid json, wrong shape"]', encoding="utf-8")
    assert store.read_all() == {}


async def test_probe_records_definitive_answers_only(tmp_path: Path) -> None:
    store = SymbolValidityStore(tmp_path)
    read = _FakeRead(
        {
            "APPL": None,
            "SPY": _asset("SPY"),
            "QQQ": BrokerError("broker down"),
        }
    )

    await _probe(store, read, ["APPL", "SPY", "QQQ"]).run_due()

    observed = store.read_all()
    assert observed["APPL"].resolvable is False
    assert observed["SPY"].resolvable is True and observed["SPY"].tradable is True
    # An unreachable broker is not evidence: QQQ stays unobserved and retire
    # for it stays fail-closed.
    assert "QQQ" not in observed


async def test_probe_never_reprobes_a_resolvable_symbol(tmp_path: Path) -> None:
    store = SymbolValidityStore(tmp_path)
    read = _FakeRead({"SPY": _asset("SPY")})
    probe = _probe(store, read, ["SPY"])

    await probe.run_due()
    await probe.run_due()

    assert read.calls == ["SPY"]


async def test_probe_refreshes_an_unresolvable_symbol_after_the_ttl(tmp_path: Path) -> None:
    store = SymbolValidityStore(tmp_path)
    clock = SimpleNamespace(now_ms=1_000_000)
    read = _FakeRead({"APPL": None})
    probe = SymbolValidityProbe(
        store=store,
        read=read,  # type: ignore[arg-type]
        roster_symbols=lambda: ["APPL"],
        now_ms=lambda: clock.now_ms,
        min_interval_ms=0,
        refresh_ttl_ms=1_000,
    )

    await probe.run_due()
    clock.now_ms += 500
    await probe.run_due()
    assert read.calls == ["APPL"], "within the TTL the negative fact is trusted"

    clock.now_ms += 500
    await probe.run_due()
    assert read.calls == ["APPL", "APPL"], "past the TTL a later listing can self-heal"


async def test_probe_min_interval_gates_the_roster_read(tmp_path: Path) -> None:
    store = SymbolValidityStore(tmp_path)
    read = _FakeRead({"APPL": None})
    roster_reads = SimpleNamespace(count=0)

    def roster() -> list[str]:
        roster_reads.count += 1
        return ["APPL"]

    clock = SimpleNamespace(now_ms=0)
    probe = SymbolValidityProbe(
        store=store,
        read=read,  # type: ignore[arg-type]
        roster_symbols=roster,
        now_ms=lambda: clock.now_ms,
        min_interval_ms=300_000,
    )

    await probe.run_due()
    clock.now_ms += 100
    await probe.run_due()
    assert roster_reads.count == 1, "a sweep pass inside the interval is a no-op"

    clock.now_ms += 300_000
    await probe.run_due()
    assert roster_reads.count == 2


async def test_probe_caps_probes_per_pass(tmp_path: Path) -> None:
    store = SymbolValidityStore(tmp_path)
    symbols = [f"S{i}" for i in range(8)]
    read = _FakeRead({symbol: _asset(symbol) for symbol in symbols})

    await _probe(store, read, symbols, max_probes_per_pass=5).run_due()

    assert len(read.calls) == 5


async def test_probe_rotates_past_symbols_that_keep_raising(tmp_path: Path) -> None:
    """A symbol that always errors records nothing and so stays due forever.

    Slicing the sorted head would re-select that same failing prefix on every
    pass and starve every later symbol indefinitely -- including the bot this
    feature exists to retire (#1904 review).
    """
    store = SymbolValidityStore(tmp_path)
    failing = [f"A{i}" for i in range(5)]
    answers: dict[str, object] = {symbol: BrokerError("down") for symbol in failing}
    answers["ZZZ"] = _asset("ZZZ")
    read = _FakeRead(answers)  # type: ignore[arg-type]
    probe = _probe(store, read, [*failing, "ZZZ"], max_probes_per_pass=5)

    await probe.run_due()
    assert "ZZZ" not in read.calls, "first pass takes the head, as before"

    await probe.run_due()

    assert "ZZZ" in read.calls, "the cursor moved past the failing prefix"
    observation = store.read("ZZZ")
    assert observation is not None and observation.resolvable is True


def test_symbol_unresolvable_for_mode_refuses_to_speak_for_dry_run(
    tmp_path: Path, monkeypatch,
) -> None:
    """Alpaca listing is not Dry Run's admission invariant (#1904 review).

    A Dry Run binding is sealed to a synthetic sim account and never contacts
    the Alpaca broker, so "not a listed Alpaca asset" proves nothing about
    whether that registration can admit again -- and must not authorise
    retiring it.
    """
    monkeypatch.setattr(
        "app.broker.alpaca.symbol_validity._store_root",
        lambda: tmp_path,
    )
    SymbolValidityStore(tmp_path).record(
        [SymbolValidityObservation(symbol="APPL", observed_at_ms=5, resolvable=False)]
    )

    assert symbol_unresolvable_for_mode("APPL", "trade") is True
    assert symbol_unresolvable_for_mode("APPL", "log_only") is True
    assert symbol_unresolvable_for_mode("APPL", "dry_run") is False
    # Membership, not exclusion: an unrecognised future mode fails closed.
    assert symbol_unresolvable_for_mode("APPL", "some_future_mode") is False


def test_symbol_marked_unresolvable_reads_the_settings_store(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.broker.alpaca.symbol_validity._store_root",
        lambda: tmp_path,
    )
    assert symbol_marked_unresolvable("APPL") is False, "no observation stays fail-closed"

    SymbolValidityStore(tmp_path).record(
        [
            SymbolValidityObservation(symbol="APPL", observed_at_ms=5, resolvable=False),
            SymbolValidityObservation(symbol="SPY", observed_at_ms=5, resolvable=True),
        ]
    )
    assert symbol_marked_unresolvable("APPL") is True
    assert symbol_marked_unresolvable("appl") is True
    assert symbol_marked_unresolvable("SPY") is False
