"""Budgeted account setup for runner and Clerk integration tests (#3286).

These suites exercise what happens after a run is admitted. The fixture
prepares their test account before the custody admission snapshot, then calls
the production registration path. Budget refusal tests build their own state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms, risk_fill_sequence
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.services.bot_binding_repository import BrokerBotBinding
from tests.broker.alpaca.clerk.sqlite.conftest import complete_fee_evidence


@pytest.fixture
def budgeted_run_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    admitted = SqliteAlpacaClerkFacade.start_admission_snapshot
    registered = SqliteAlpacaClerkFacade.register_strategy_run
    prepared_repos: set[object] = set()

    def prepare(self: SqliteAlpacaClerkFacade) -> None:
        repo = self.repository
        if repo not in prepared_repos:
            commit_budget_authority_cutover(repo, actor="owner", reviewed_token="test-account", stop_receipt="no-legacy-runs")
            if self.authority_kind != "synthetic" and repo.account_risk_policy() is None:
                append_risk_policy(repo, policy=AccountRiskPolicy(
                    1, .1, 100_000, "test-profile", 1, "owner", repo.clock(),
                ), expected_revision=0)
            if self.authority_kind == "sqlite":
                complete_fee_evidence(repo)
            prepared_repos.add(repo)
        now = repo.clock()
        policy = repo.account_risk_policy()
        risk_revision = None if policy is None else policy.revision
        gate = self.live_envelope or LiveEnvelopeGate(values=None, custody_is_simulated=self.authority_kind != "sqlite")
        if self._simulation is not None:
            if self.authority_kind == "synthetic" and self._simulation.initial_cash is None:
                self._simulation.initial_cash = Decimal("100000")
            observation = replace(
                self._simulation.observe(reference_cash=1_000_000, observed_at_ms=now, now_ms=now),
                risk_revision=risk_revision,
            )
        else:
            observation = AccountObservation(
                observed_at_ms=now, broker_cash_usd=1_000_000, cash_available_usd=1_000_000,
                equity_usd=1_000_000, last_equity_usd=1_000_000, position_count=0,
                risk_revision=risk_revision, risk_fill_sequence=risk_fill_sequence(repo),
                risk_cash_flow_evidence_complete=True,
                risk_cash_flow_window_start_ms=day_pnl_window_start_ms(now),
                risk_equity_window_start_ms=day_pnl_window_start_ms(now),
                simulation_session_start_ms=et_day_window_ms(now)[0] if self.authority_kind != "sqlite" else None,
            )
        gate.publish(observation)
        self._live_envelope = gate

    @asynccontextmanager
    async def start_admission_snapshot(
        self: SqliteAlpacaClerkFacade, strategy_instance_id: str,
    ) -> AsyncIterator[object]:
        prepare(self)
        async with admitted(self, strategy_instance_id) as snapshot:
            yield snapshot

    async def register_strategy_run(
        self: SqliteAlpacaClerkFacade, binding: BrokerBotBinding, *,
        admission_snapshot: object = None, run_owner: object = None,
    ) -> None:
        prepare(self)
        original_quote_source = self._quote_source
        if original_quote_source(binding.symbol, self.repository.clock()) is None:
            self._quote_source = lambda _symbol, _now: SimpleNamespace(ask=100)
        try:
            await registered(self, binding, admission_snapshot=admission_snapshot, run_owner=run_owner)
        finally:
            self._quote_source = original_quote_source

    monkeypatch.setattr(SqliteAlpacaClerkFacade, "start_admission_snapshot", start_admission_snapshot)
    monkeypatch.setattr(SqliteAlpacaClerkFacade, "register_strategy_run", register_strategy_run)
