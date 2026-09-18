/** #2202 (PRD #2201 §11.1/§11.2): a focused regression spec for the
 * Operator-lens journal-failure path, kept out of the ~1100-line
 * `operator-lens.component.spec.ts` so this file stays quick to scan and the
 * shard it lands in stays inside the two-minute test-gate budget.
 *
 * Before the fix, `operator-lens.component.html` read
 * `journalPage.value() ?? null` while `journalPage` was in its error state.
 * That throws `ResourceValueError` during Angular's render pass, and the
 * whole pass — health card, Clerk card, working orders, run history, action
 * controls, all siblings of the journal disclosure in the same template —
 * is abandoned (#2201 §2.1, same defect class as the Trader-lens history
 * freeze). Confirmed by hand: reverting the `hasValue()` guard on
 * `operator-lens.component.html`'s `journalPage` binding makes the case
 * below fail with an uncaught `ResourceValueError` instead of rendering
 * "Could not load journal evidence."
 */
import { render, screen, fireEvent, within } from '@testing-library/angular';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import type { BotHealthCard, BotPanelView, ClerkCard, PanelProfile } from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { MarketDataService } from '../../../../services/market-data.service';
import { OperatorLensComponent } from './operator-lens.component';
import { provideFleetDirectory } from '../../../../fleet/fleet-directory-testing';

function makeHealth(): BotHealthCard {
  return {
    strategy_instance_id: 'sid-1',
    phase: 'ON_DUTY',
    phase_label: 'Live',
    desired_state: 'RUNNING',
    desired_state_label: 'Running',
    running: true,
    duty_outcome: null,
    last_decision_at_ms: 1_700_000_000_000,
    decision_stale: false,
    last_bar_at_ms: 1_700_000_001_000,
    resume_eligible: false,
    resume_label: 'Resume not applicable',
    resume_explanation: 'This strategy instance already has a live run.',
    carryover_checkpoint_exposure: {},
  };
}

function makeClerk(): ClerkCard {
  return {
    account_id: 'acc-1',
    hold_active: false,
    hold_reason: 'NO_HOLD',
    hold_reason_label: 'No hold',
    hold_reason_explanation: '',
    hold_since_ms: null,
    freeze_active: false,
    freeze_category: null,
    freeze_label: 'No account freeze',
    freeze_explanation: 'Account truth is current.',
    freeze_next_step: null,
    freeze_observed_at_ms: null,
    reconciliation_verdict: null,
    reconciliation_verdict_label: null,
    last_sweep_at_ms: null,
    outstanding_intents: 0,
    channels: [],
  };
}

function makePanel(): BotPanelView {
  return {
    strategy_instance_id: 'sid-1',
    strategy_key: 'ema_crossover',
    strategy_label: 'Ema Crossover',
    broker: 'alpaca',
    account_id: 'acc-1',
    symbol: 'SPY',
    mode: 'log_only',
    sealed_program: null,
    program_build: {
      state: 'NOT_APPLICABLE',
      program_key: 'ema_crossover',
      verified_at_ms: 1_700_000_001_000,
      explanation: 'No Signal Program build proof supplied.',
    },
    resume_admission: null,
    updated_at_ms: 1_700_000_001_000,
    revision: 1,
    market_pulse: {
      session: 'OPEN',
      market_state: 'TRADABLE',
      market_liveness_reason: 'Fresh test evidence proves tradability.',
      market_liveness_observed_at_ms: 1_700_000_001_000,
      halted_symbol: null,
      feed_state: 'LIVE',
      latest_bar_at_ms: 1_700_000_000_000,
      age_ms: 1_000,
      source: 'ibkr',
      expected_cadence_ms: 60_000,
      headline: 'Market data live',
      explanation: 'The feed is current.',
      next_step: null,
      attention_required: false,
      observed_at_ms: 1_700_000_001_000,
    },
    feed_continuity: {
      provider_label: 'IBKR market data',
      run_id: 'run-1',
      state: 'continuous',
      state_label: 'Continuous',
      explanation: 'No IBKR delivery interruptions have been recorded in this run.',
      interruption_count: 0,
      recovery_count: 0,
      unresolved_count: 0,
      decision_impact_count: 0,
      last_interruption_at_ms: null,
      last_recovery_at_ms: null,
      latest_bar_at_ms: 1_700_000_000_000,
      events: [],
    },
    mission_verdict: {
      state: 'working',
      label: 'Working',
      explanation: 'The runtime is on duty.',
      next_action: 'Monitor evidence.',
      evaluated_at_ms: 1_700_000_001_000,
    },
    execution_policy: 'Observation only.',
    health: makeHealth(),
    clerk: makeClerk(),
    rail: { transaction_ref: 'tx-001', stations: [] },
    journal_tail_ref: '',
    journal_tail_seq: null,
    actions: [],
    primary_action_by_lens: { trader: null, operator: null },
    readiness_checks: [],
    readiness_ready_count: 0,
    readiness_blocked_count: 0,
    exposure: {},
    working_orders: [],
    recent_decisions: [],
    recent_fills: [],
    fills_today: 0,
    realized_pnl_today: 0.0,
    open_pnl: null,
  };
}

function makeProfile(): PanelProfile {
  return {
    broker: 'alpaca',
    fee_fidelity: 'none',
    flatten_supported: true,
    live_bars_supported: true,
    stations: [],
    supported_action_ids: [],
  };
}

function openDisclosure(label: string): void {
  const details = screen.getByText(label).closest('details');
  if (details === null) throw new Error(`Expected ${label} disclosure.`);
  details.open = true;
  fireEvent(details, new Event('toggle'));
}

// Matches the parent spec's DI setup (`operator-lens.component.spec.ts`): a
// ready fleet lane and a market-data double so `panel.symbol`'s snapshot
// resource has something to resolve without a real HTTP call.
beforeEach(() => {
  TestBed.configureTestingModule({
    providers: [
      provideFleetDirectory(),
      {
        provide: MarketDataService,
        useValue: { getStockSnapshot: () => of({ success: true, snapshot: null, error: null }) },
      },
    ],
  });
});

describe('OperatorLensComponent — journal failure isolation (#2202)', () => {
  it('an errored journal read shows its own error and does not freeze the rest of the Operator lens', async () => {
    const fakeSvc = {
      getEvidence: vi.fn().mockRejectedValue(new Error('journal evidence unavailable')),
      getCurrentRun: vi.fn().mockRejectedValue(new Error('No current run fixture.')),
      getRunHistory: vi.fn().mockResolvedValue({ runs: [], next_cursor: null }),
    };

    const { fixture } = await render(OperatorLensComponent, {
      inputs: {
        clerkId: 'clrk_spec',
        panel: makePanel(),
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    // Sibling regions render before the journal is even opened — the panel
    // was never gated on the journal resource.
    expect(within(screen.getByLabelText('Bot health')).getByText('Live')).toBeTruthy();

    openDisclosure('Audit trail');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByRole('alert').textContent).toContain('Could not load journal evidence.');

    // The rest of the lens is still live and interactive after the journal
    // settled into its error state — nothing downstream of the throw was
    // dropped from this render pass.
    expect(within(screen.getByLabelText('Bot health')).getByText('Live')).toBeTruthy();
    expect(screen.getByText(/No Clerk-attributed working orders/)).toBeTruthy();
  });
});
