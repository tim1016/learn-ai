"""LEAN-parity EMA-crossover bar fixture and the Signal Program's
deterministic per-bar evaluation identity, shared by the bot_runner test
package and downstream replay/crash suites.

Split out of ``tests/services/bot_runner/conftest.py`` per issue #1810 --
see that module's sibling ``doubles.py``/``custody.py``/``market.py`` for
the other extracted themes.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from app.engine.strategy.programs.ema_crossover_signal import EmaCrossoverSignalParams
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.marketdata.feed import MarketDataBar

if TYPE_CHECKING:
    import pytest

_EMA_FIRST_EXIT_MS = 1_770_393_600_000


def _ema_parity_bars_through_first_exit() -> list[MarketDataBar]:
    """Load the retained LEAN input stream through its first EMA round-trip."""
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures/golden/cross-engine-studies/cells"
        / "SPY_W3mo_2026-02-02_to_2026-04-30/lean/observations.csv"
    )
    bars: list[MarketDataBar] = []
    with fixture.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            end_ms = int(row["ms_utc"])
            bars.append(
                MarketDataBar(
                    symbol="SPY",
                    start_ms=end_ms - 60_000,
                    end_ms=end_ms,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=int(Decimal(row["volume"])),
                    fetched_at_ms=end_ms + 100,
                    feed_id="lean-golden",
                    session_phase="RTH",
                )
            )
            if end_ms > _EMA_FIRST_EXIT_MS:
                break
    return bars


def _ema_signal_evaluation_id(bar_close_ms: int, *, symbol: str = "SPY") -> str:
    """Independently recompute the Signal Program's documented evaluation
    identity (see the Formula note in ``app/engine/strategy/signal_program.py``:
    SHA-256 of the canonical JSON of program version, settings, and bar-close
    clock) from the real registered strategy -- not a hand-typed guess at the
    hash bytes. Proves ``decision_id`` really is the deterministic per-bar
    Signal Program identity the PRD requires (``decision_id = evaluation_id``,
    issue #1728 / PRD section 16), rather than merely echoing whatever the
    current build happens to emit.
    """
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    assert registration.signal_program_factory is not None
    params = registration.param_schema(symbol=symbol)
    program = registration.signal_program_factory(params)
    payload = {
        "program_key": program.session.program_key,
        "program_version": program.session.program_version,
        "settings": program.strategy.signal_program_settings(),
        "bar_close_ms": bar_close_ms,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def admit_lean_parity_settings_for_start_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Temporarily register the ENG-007 LEAN-parity point as this process's
    golden-qualification ``validated_settings`` for ``ema_crossover_signal``.

    Since the 2026-09-01 move (registry.py's ``validated_settings`` /
    ``tests/fixtures/golden/ema-signal-session/v1/attribution.md``'s
    "Regeneration 2026-09-01"), the registry's deploy-admission point is the
    relaxed one (gap=0.0, rsi_min=30.0) -- correct for real deploy
    admission, but it means a bot deployed at ``EmaCrossoverSignalParams``'s
    own defaults (the LEAN-parity point ``gap=0.20``/``rsi_min=50.0`` that
    ``docs/references/reconciliations/ema-crossover-signal-lean-2026-07-18.md``
    and ``_ema_parity_bars_through_first_exit`` are pinned to) is stamped
    ``corpus_coverage="UNCOVERED"`` rather than covered (ADR 0054) -- and,
    outside a proven paper environment, refused ``PROGRAM_CORPUS_UNCOVERED``
    -- even though that point is, if anything, the *most* rigorously proven
    one of all.

    Tests using this bypass aren't exercising deploy admission; they're
    proving bot-runner mechanics or LEAN-parity math against the pinned
    fixture, which only reconciles at the LEAN-parity point. Re-registering
    it as "validated" for the process lifetime of one test is the same
    move ``admit_canary_pairing`` makes for the canary allowlist: keep one
    unrelated stamp off the run so the rest of the test runs as covered
    evidence.
    """
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    assert registration.signal_program_contract is not None
    lean_parity_settings = {
        name: EmaCrossoverSignalParams.model_fields[name].default
        for name in ("gap", "gap_bps", "rsi_min", "rsi_max")
    }
    monkeypatch.setattr(
        registration,
        "signal_program_contract",
        dataclasses.replace(
            registration.signal_program_contract,
            validated_settings=lean_parity_settings,
        ),
    )
