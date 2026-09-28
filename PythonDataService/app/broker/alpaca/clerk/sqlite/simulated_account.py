"""One simulated cash/marked-risk projection for Shadow and private Dry Runs.

Formula: cash = reference capital - effective BUY costs + effective SELL
  proceeds - modelled settled fees; unrealized delegates to canonical FIFO;
  baseline = retained initial capital + prior gross realized + prior open P&L
  - prior modelled fees, using the canonical previous-session close marks.
Reference: PRD #2540 world isolation; ADR 0059 simulation-risk amendment.
Canonical implementation: this composition; FIFO and fees retain their existing
  authorities. No balance, position or fee ledger is introduced.
Validated against: tests/broker/alpaca/clerk/sqlite/test_simulated_account.py.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.broker.alpaca.clerk.account_authority import shadow_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.alpaca.clerk.models import EpochMs
from app.broker.alpaca.clerk.money import ZERO, money_context, normalize_money, notional
from app.broker.alpaca.clerk.sqlite.day_pnl import risk_fill_sequence
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import OrderSide
from app.lean_sidecar.trading_calendar import previous_completed_session_close_ms
from app.marketdata.feed import DELIVERY_ALLOWANCE_MS
from app.services.decision_clock import SOURCE_BAR_MS
from app.services.source_bar_ledger import SourceBarLedger, SourceBarLedgerCorruptError, SourceBarLedgerMissingError

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

BASELINE_KIND = "SIMULATION_SESSION_BASELINE"


class SimulationEvidenceUnavailable(BrokerUnavailable):
    """A simulated world cannot prove its cash, marks or retained risk baseline."""


class SimulationBaseline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_start_ms: EpochMs
    initial_capital_usd: Decimal
    equity_usd: Decimal
    observed_at_ms: EpochMs
    mark_cutoff_ms: EpochMs
    mark_refs: tuple[str, ...] = ()

    @field_validator("initial_capital_usd", "equity_usd")
    @classmethod
    def finite_money(cls, value: Decimal) -> Decimal:
        return normalize_money(value)


def fold_simulation_baseline(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Validate replayed evidence; custody transitions themselves retain it."""
    account_id = conn.execute("SELECT account_id FROM control_meta WHERE id=1").fetchone()[0]
    if not account_id.startswith(("sim:", "shadow:")):
        raise ValueError("Simulation baseline cannot enter real custody")
    SimulationBaseline.model_validate_json(payload["facts_json"])


class SimulatedAccountProjection:
    def __init__(self, *, repo: ClerkSqliteRepository, artifacts_root: Path, initial_cash: Decimal | None = None) -> None:
        if not repo.account_id.startswith(("sim:", "shadow:")):
            raise ValueError("Simulated projection requires a simulated custody account")
        self.repo, self.artifacts_root = repo, artifacts_root
        self.initial_cash = initial_cash

    def _private_cash(self) -> tuple[Decimal, bool]:
        rows = self.repo._conn.execute("SELECT committed_cents FROM deployment_budgets").fetchall()
        if len(rows) > 1:
            raise SimulationEvidenceUnavailable("A private Dry Run has conflicting starting-cash commitments.")
        if rows:
            return Decimal(rows[0][0]) / 100, True
        if self.initial_cash is None:
            raise SimulationEvidenceUnavailable("Choose simulated starting cash before deploying this Dry Run.")
        return normalize_money(self.initial_cash), False

    def _marks(self, records: tuple[FillRecord, ...], *, at_ms: int, exact_close: bool = False) -> tuple[dict[str, float], tuple[str, ...], int | None]:
        open_symbols = {lot.symbol for lot in compute_fifo_pnl(records).open_lots}
        marks: dict[str, float] = {}
        refs: list[str] = []
        valid_until: int | None = None
        for symbol in open_symbols:
            if self.repo.account_id.startswith("sim:"):
                accounts = {self.repo.account_id}
                provider = None
            else:
                accounts = {
                    shadow_evidence_account_id_for_strategy(fill.sid.removeprefix("bot:"))
                    for fill in records if fill.symbol == symbol and fill.sid.startswith("bot:")
                }
                provider = "ibkr"
            candidates = []
            for account in sorted(accounts):
                try:
                    ledger = SourceBarLedger(artifacts_root=self.artifacts_root, account_id=account, read_only=True)
                except SourceBarLedgerMissingError:
                    continue
                except SourceBarLedgerCorruptError as exc:
                    raise SimulationEvidenceUnavailable("The simulation price evidence needs repair.") from exc
                try:
                    bar = ledger.latest_for_symbol(symbol, provider=provider, at_or_before_ms=at_ms)
                finally:
                    ledger.close(checkpoint=False)
                if bar is not None:
                    candidates.append(bar)
            valid = [bar for bar in candidates if (bar.end_ms == at_ms if exact_close else 0 <= at_ms - bar.end_ms <= SOURCE_BAR_MS + DELIVERY_ALLOWANCE_MS)]
            if not valid:
                raise SimulationEvidenceUnavailable(f"Fresh simulated price evidence for {symbol} is unavailable. Wait for its market feed.")
            latest_at = max(bar.end_ms for bar in valid)
            latest = [bar for bar in valid if bar.end_ms == latest_at]
            if len({bar.close for bar in latest}) != 1:
                raise SimulationEvidenceUnavailable(f"Simulated price evidence for {symbol} disagrees across retained streams.")
            price = normalize_money(latest[0].close)
            if price <= ZERO:
                raise SimulationEvidenceUnavailable(f"Simulated price evidence for {symbol} is not positive.")
            marks[symbol] = float(price)
            refs.append(latest[0].bar_ref)
            expires = latest_at + SOURCE_BAR_MS + DELIVERY_ALLOWANCE_MS
            valid_until = expires if valid_until is None else min(valid_until, expires)
        return marks, tuple(refs), valid_until

    def _baseline(self, records: tuple[FillRecord, ...], *, capital: Decimal, now_ms: int, persist: bool) -> SimulationBaseline:
        session_start, _ = et_day_window_ms(now_ms)
        rows = [SimulationBaseline.model_validate_json(row[0]) for row in self.repo._conn.execute(
            "SELECT facts_json FROM custody_transitions WHERE transition_kind=? ORDER BY sequence", (BASELINE_KIND,),
        )]
        same_session = [row for row in rows if row.session_start_ms == session_start]
        if same_session:
            return same_session[-1]
        if not rows:
            if capital <= ZERO:
                raise SimulationEvidenceUnavailable("Positive reference capital is required to establish the simulation's initial baseline.")
            initial_at = now_ms
            if self.repo.account_id.startswith("sim:") and persist:
                committed = self.repo._conn.execute(
                    "SELECT b.committed_at_ms,t.sequence FROM deployment_budgets b JOIN custody_transitions t ON t.command_id=b.command_id WHERE t.transition_kind='DEPLOY_COMMITTED'"
                ).fetchone()
                if committed is None or any(fill.ledger_sequence <= committed["sequence"] for fill in records):
                    raise SimulationEvidenceUnavailable("The private simulation cannot prove its initial budget preceded every fill.")
                initial_at = committed["committed_at_ms"]
            elif records:
                raise SimulationEvidenceUnavailable("The simulation has fills but no retained initial risk capital. Review its original activation evidence.")
            baseline = SimulationBaseline(session_start_ms=et_day_window_ms(initial_at)[0], initial_capital_usd=capital,
                equity_usd=capital, observed_at_ms=now_ms, mark_cutoff_ms=initial_at)
        else:
            if session_start < rows[-1].session_start_ms:
                raise SimulationEvidenceUnavailable("The simulation clock predates its retained session baseline.")
            # The latest scheduled prior close is supplied by the one NYSE
            # calendar, including holidays, DST and early-close sessions.
            cutoff = previous_completed_session_close_ms(session_start)
            prior = tuple(record for record in records if record.filled_at_ms < session_start)
            marks, refs, _ = self._marks(prior, at_ms=cutoff, exact_close=True)
            fifo = compute_fifo_pnl(prior, mark_prices=marks)
            fees = custody_fee_attribution(self.repo._conn, now_ms=now_ms, to_ms=session_start)
            if not fees.known or fifo.open_pnl is None:
                raise SimulationEvidenceUnavailable("The prior simulation session cannot prove its equity baseline.")
            initial = rows[0].initial_capital_usd
            equity = initial + normalize_money(fifo.realized_pnl) + normalize_money(fifo.open_pnl) - sum((share.amount for share in fees.shares), ZERO)
            baseline = SimulationBaseline(session_start_ms=session_start, initial_capital_usd=initial,
                equity_usd=equity, observed_at_ms=now_ms, mark_cutoff_ms=cutoff, mark_refs=refs)
        if persist:
            self.repo.append_transition(TransitionInput(
                transition_kind=BASELINE_KIND, custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
                operation_state="succeeded", clerk_observed_at_ms=now_ms, summary_code=BASELINE_KIND,
                facts_json=canonicalize(baseline.model_dump(mode="json")),
            ))
        if baseline.session_start_ms != session_start:
            return self._baseline(records, capital=capital, now_ms=now_ms, persist=persist)
        return baseline

    @money_context()
    def observe(self, *, reference_cash: object, observed_at_ms: int, now_ms: int) -> AccountObservation:
        """One synchronous custody-fenced projection; performs no broker calls.

        Dry Run uses durable committed cents, or explicit transient consent
        before that command commits. Shadow uses current real cash only as
        available reference capital; its retained risk capital never resets.
        """
        with self.repo._write_lock:
            private = self.repo.account_id.startswith("sim:")
            capital, persist = self._private_cash() if private else (normalize_money(reference_cash), True)
            records = effective_fill_records(self.repo._conn, account_id=self.repo.account_id)
            fees = custody_fee_attribution(self.repo._conn, now_ms=now_ms)
            if not fees.known:
                raise SimulationEvidenceUnavailable("Simulated execution or modelled fee evidence is incomplete: " + "; ".join(fees.unresolved))
            marks, _, valid_until = self._marks(records, at_ms=now_ms)
            fifo = compute_fifo_pnl(records, mark_prices=marks)
            if fifo.open_pnl is None:
                raise SimulationEvidenceUnavailable("Simulated open P&L cannot be valued from current price evidence.")
            baseline = self._baseline(records, capital=capital, now_ms=now_ms, persist=persist)
            spending = sum((notional(fill.quantity, fill.fill_price) * (1 if fill.side is OrderSide.BUY else -1) for fill in records), ZERO)
            settled = sum((share.amount for share in fees.shares if share.state == "modelled_settled"), ZERO)
            cash = capital - spending - settled
            return AccountObservation(
                observed_at_ms=observed_at_ms, broker_cash_usd=float(capital), cash_available_usd=cash,
                last_equity_usd=float(baseline.equity_usd), unrealized_pl_usd=fifo.open_pnl,
                position_count=len(fifo.open_lots), risk_fill_sequence=risk_fill_sequence(self.repo),
                simulation_cash_seen_before_ms=now_ms + 1, modelled_fees_seen_before_ms=now_ms + 1,
                simulation_session_start_ms=baseline.session_start_ms, simulation_marks_valid_until_ms=valid_until,
            )
