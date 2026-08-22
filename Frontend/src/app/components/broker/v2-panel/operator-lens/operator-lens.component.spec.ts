import { render, screen, fireEvent, within } from '@testing-library/angular';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import type {
  BotHealthCard,
  BotPanelView,
  ClerkCard,
  EvidencePage,
  PanelAction,
  PanelProfile,
  TransactionRail,
} from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { MarketDataService } from '../../../../services/market-data.service';
import { OperatorLensComponent } from './operator-lens.component';

// ── Minimal test data factories ───────────────────────────────────────────────

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

function makeRail(): TransactionRail {
  return {
    transaction_ref: 'tx-001',
    stations: [
      {
        station_id: 'SIGNAL',
        label: 'Signal',
        state: 'satisfied',
        state_label: 'Ready',
        receipt: '',
        evidence_at_ms: null,
        blocker: null,
      },
    ],
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
    rail: makeRail(),
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

function makeReadinessCheck(
  action: PanelAction,
  overrides: Partial<BotPanelView['readiness_checks'][number]> = {},
): BotPanelView['readiness_checks'][number] {
  return {
    operation: action.action_id,
    label: action.label,
    ready: action.enabled,
    scope: 'bot',
    authority: 'Bot lifecycle registry + Alpaca Clerk',
    explanation: action.explanation,
    evidence: {},
    evaluated_at_ms: 1_700_000_001_000,
    cure: null,
    ...overrides,
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

function expandReadiness(label: string): HTMLElement {
  const header = screen.getByRole('button', {
    name: new RegExp(`(?:Ready|Blocked) ${label}`, 'i'),
  });
  if (header.getAttribute('aria-expanded') !== 'true') fireEvent.click(header);
  return header;
}

function openDisclosure(label: string): void {
  const details = screen.getByText(label).closest('details');
  if (details === null) throw new Error(`Expected ${label} disclosure.`);
  details.open = true;
  fireEvent(details, new Event('toggle'));
}

function closeDisclosure(label: string): void {
  const details = screen.getByText(label).closest('details');
  if (details === null) throw new Error(`Expected ${label} disclosure.`);
  details.open = false;
  fireEvent(details, new Event('toggle'));
}

function makeEvidencePage(): EvidencePage {
  return {
    strategy_instance_id: 'sid-1',
    account_id: 'acc-1',
    transaction_ref: 'tx-001',
    entries: [
      {
        seq: 1,
        kind: 'ORDER_SUBMITTED',
        kind_label: 'Order submitted',
        recorded_at_ms: 1_700_000_000_000,
        order_ref: 'tx-001',
        intent_id: null,
        summary: 'BUY 10 SPY @ market',
        has_more_detail: false,
      },
    ],
    next_cursor: null,
    total_entries: 1,
    truncated: false,
    read_by: 'operator:system',
    read_at_ms: 1_700_000_001_000,
  };
}

// ── Fake service ───────────────────────────────────────────────────────────────

function makeFakePanelService(evidencePage?: EvidencePage) {
  return {
    getEvidence: vi.fn(
      () => Promise.resolve(evidencePage ?? makeEvidencePage()),
    ) as unknown as (broker: string, accountId: string, sid: string, params: Record<string, unknown>) => Promise<EvidencePage>,
    getCurrentRun: vi.fn().mockRejectedValue(new Error('No current run fixture.')),
    getRunHistory: vi.fn().mockResolvedValue({ runs: [], next_cursor: null }),
  };
}

beforeEach(() => {
  TestBed.configureTestingModule({
    providers: [
      {
        provide: MarketDataService,
        useValue: {
          getStockSnapshot: () => of({ success: true, snapshot: null, error: null }),
        },
      },
    ],
  });
});

// ── Tests ──────────────────────────────────────────────────────────────────────

describe('OperatorLensComponent', () => {
  it('renders server-authored readiness totals without recomputing them', async () => {
    const fakeSvc = makeFakePanelService();
    const panel: BotPanelView = {
      ...makePanel(),
      readiness_ready_count: 7,
      readiness_blocked_count: 3,
    };

    await render(OperatorLensComponent, {
      inputs: {
        panel,
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expect(screen.getByText(/7 ready · 3 blocked/)).toBeTruthy();
  });

  it('renders the health card phase label', async () => {
    const fakeSvc = makeFakePanelService();

    await render(OperatorLensComponent, {
      inputs: {
        panel: makePanel(),
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expect(within(screen.getByLabelText('Bot health')).getByText('Live')).toBeTruthy();
  });

  it('renders run evidence as the final operator section', async () => {
    const fakeSvc = makeFakePanelService();
    const { container } = await render(OperatorLensComponent, {
      inputs: {
        panel: makePanel(),
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    const runHistory = container.querySelector('app-operator-run-history');
    expect(runHistory).not.toBeNull();
    expect(runHistory?.nextElementSibling).toBeNull();
    expect(screen.getByText('Run evidence')).toBeTruthy();
    expect(screen.getByText('Current and previous runs')).toBeTruthy();
    expect(container.querySelector('app-bot-run-history')).toBeNull();
  });

  it('renders backend-authored Resume and account-freeze copy unchanged', async () => {
    const fakeSvc = makeFakePanelService();
    const panel: BotPanelView = {
      ...makePanel(),
      health: {
        ...makeHealth(),
        running: false,
        phase: 'OFF_DUTY',
        desired_state: 'STOPPED',
        resume_eligible: false,
        resume_label: 'opaque resume label 01J9',
        resume_explanation: 'Exact carryover comparison failed at the Clerk.',
      },
      clerk: {
        ...makeClerk(),
        freeze_active: true,
        freeze_category: 'ACCOUNT_STATE_UNPROVABLE',
        freeze_label: 'opaque freeze label 01J8',
        freeze_explanation: 'Fresh broker truth could not be established.',
        freeze_next_step: 'Restore observation and reconcile.',
        freeze_observed_at_ms: 1_700_000_002_000,
      },
    };

    await render(OperatorLensComponent, {
      inputs: {
        panel,
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expect(screen.getByText('opaque resume label 01J9')).toBeTruthy();
    expect(
      screen.getByText('Exact carryover comparison failed at the Clerk.'),
    ).toBeTruthy();
    expect(screen.getByText('opaque freeze label 01J8')).toBeTruthy();
    expect(
      screen.getByText('Fresh broker truth could not be established.'),
    ).toBeTruthy();
    expect(screen.getByText('Restore observation and reconcile.')).toBeTruthy();
  });

  it('keeps the promoted lifecycle action out of readiness while retaining its gate', async () => {
    const fakeSvc = makeFakePanelService();
    const resumeAction: PanelAction = {
      action_id: 'resume', label: 'Resume', explanation: 'Resume bot.', enabled: true,
      blockers: [], confirmation: null, revision: 1, concurrency_token: 'resume-token',
    };
    const panel: BotPanelView = {
      ...makePanel(),
      health: { ...makeHealth(), running: false, phase: 'OFF_DUTY', desired_state: 'STOPPED' },
      actions: [resumeAction],
      primary_action_by_lens: { trader: 'resume', operator: 'resume' },
      readiness_checks: [makeReadinessCheck(resumeAction)],
    };

    await render(OperatorLensComponent, {
      inputs: {
        panel, profile: makeProfile(), actionPending: false,
        broker: 'alpaca', accountId: 'acc-1', sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expect(screen.getAllByRole('button', { name: 'Resume' })).toHaveLength(1);
    expect(screen.getByRole('button', { name: /Ready Resume/i })).toBeTruthy();
  });

  it('renders the transaction rail with the station from the panel', async () => {
    const fakeSvc = makeFakePanelService();

    await render(OperatorLensComponent, {
      inputs: {
        panel: makePanel(),
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expect(screen.getByText('Signal')).toBeTruthy();
  });

  it('loads the journal only after the collapsed audit trail is opened', async () => {
    const fakeSvc = makeFakePanelService();

    const { fixture } = await render(OperatorLensComponent, {
      inputs: {
        panel: makePanel(),
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expect(fakeSvc.getEvidence).not.toHaveBeenCalled();
    openDisclosure('Audit trail');
    await fixture.whenStable();

    expect(fakeSvc.getEvidence).toHaveBeenCalledWith(
      'alpaca',
      'acc-1',
      'sid-1',
      expect.objectContaining({ clientHint: 'operator-lens-journal-tail' }),
    );

    closeDisclosure('Audit trail');
    openDisclosure('Audit trail');
    await fixture.whenStable();
    expect(fakeSvc.getEvidence).toHaveBeenCalledTimes(1);
  });

  it('journal tail shows loaded entries', async () => {
    const fakeSvc = makeFakePanelService();

    const { fixture } = await render(OperatorLensComponent, {
      inputs: {
        panel: makePanel(),
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    openDisclosure('Audit trail');
    await fixture.whenStable();

    const matches = await screen.findAllByText('Order submitted');
    expect(matches.length).toBeGreaterThan(0);
    expect(screen.getByText('BUY 10 SPY @ market')).toBeTruthy();
  });

  it('flatten-stop button is present when the action is in the panel', async () => {
    const fakeSvc = makeFakePanelService();
    const flattenAction: PanelAction = {
      action_id: 'flatten_stop',
      label: 'Flatten & Stop',
      explanation: 'Flattens all positions and stops the bot.',
      enabled: true,
      blockers: [],
      confirmation: null,
      revision: 1,
      concurrency_token: 'test-token',
    };
    const panel: BotPanelView = {
      ...makePanel(),
      actions: [flattenAction],
      readiness_checks: [makeReadinessCheck(flattenAction)],
      readiness_ready_count: 1,
    };

    await render(OperatorLensComponent, {
      inputs: {
        panel,
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expandReadiness('Flatten & Stop');
    expect(await screen.findByRole('button', { name: 'Flatten & Stop' })).toBeTruthy();
  });

  it('renders the SQLite stop-decisions action in its readiness gate', async () => {
    const fakeSvc = makeFakePanelService();
    const stopAction: PanelAction = {
      action_id: 'stop_bot_decisions',
      label: 'Stop bot decisions',
      explanation: 'Stop new decisions after the durable SQLite command.',
      enabled: true,
      blockers: [],
      confirmation: null,
      revision: 2,
      concurrency_token: 'sqlite-stop-token',
    };
    const value: BotPanelView = {
      ...makePanel(),
      actions: [stopAction],
      readiness_checks: [makeReadinessCheck(stopAction)],
      readiness_ready_count: 1,
    };

    await render(OperatorLensComponent, {
      inputs: {
        panel: value,
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expandReadiness('Stop bot decisions');
    const stopButton = await screen.findByRole('button', { name: 'Stop bot decisions' });
    expect(stopButton.classList.contains('panel-action__button--danger')).toBe(true);
  });

  it('flatten-stop button is disabled when action is disabled', async () => {
    const fakeSvc = makeFakePanelService();
    const flattenAction: PanelAction = {
      action_id: 'flatten_stop',
      label: 'Flatten & Stop',
      explanation: 'Not available.',
      enabled: false,
      blockers: [],
      confirmation: null,
      revision: 1,
      concurrency_token: 'test-token',
    };
    const panel: BotPanelView = {
      ...makePanel(),
      actions: [flattenAction],
      readiness_checks: [makeReadinessCheck(flattenAction)],
      readiness_blocked_count: 1,
    };

    await render(OperatorLensComponent, {
      inputs: {
        panel,
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expandReadiness('Flatten & Stop');
    const btn = await screen.findByRole('button', { name: 'Flatten & Stop' });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });

  it('clicking flatten-stop calls actionRequested with the action', async () => {
    const fakeSvc = makeFakePanelService();
    const onActionRequested = vi.fn() as unknown as (
      trigger: { action: PanelAction; reason: string | null },
    ) => void;
    const flattenAction = {
      action_id: 'flatten_stop' as const,
      label: 'Flatten & Stop',
      explanation: '',
      enabled: true,
      blockers: [],
      confirmation: null,
      revision: 2,
      concurrency_token: 'test-token',
    };
    const panel: BotPanelView = {
      ...makePanel(),
      actions: [flattenAction],
      readiness_checks: [makeReadinessCheck(flattenAction)],
      readiness_ready_count: 1,
    };

    const { fixture } = await render(OperatorLensComponent, {
      inputs: {
        panel,
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      on: { actionRequested: onActionRequested },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expandReadiness('Flatten & Stop');
    fireEvent.click(await screen.findByRole('button', { name: 'Flatten & Stop' }));
    await fixture.whenStable();

    expect(onActionRequested).toHaveBeenCalledWith({ action: flattenAction, reason: null });
  });

  it('has no sidebar evidence drawer', async () => {
    const fakeSvc = makeFakePanelService();
    const { container } = await render(OperatorLensComponent, {
      inputs: {
        panel: makePanel(),
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expect(container.querySelector('.evidence-drawer')).toBeNull();
    expect(container.querySelector('.evidence-drawer__backdrop')).toBeNull();
  });

  it('shows confirmation prompt when flatten-stop has confirmation.required', async () => {
    const fakeSvc = makeFakePanelService();
    const flattenAction: PanelAction = {
      action_id: 'flatten_stop',
      label: 'Flatten & Stop',
      explanation: '',
      enabled: true,
      blockers: [],
      confirmation: {
        title: 'Flatten and stop',
        body: 'This will close all open positions.',
        consequence: 'Attributed exposure will be reduced to zero.',
        confirm_label: 'Flatten and stop',
      },
      revision: 1,
      concurrency_token: 'test-token',
    };
    const panel: BotPanelView = {
      ...makePanel(),
      actions: [flattenAction],
      readiness_checks: [makeReadinessCheck(flattenAction)],
      readiness_ready_count: 1,
    };

    await render(OperatorLensComponent, {
      inputs: {
        panel,
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expandReadiness('Flatten & Stop');
    fireEvent.click(await screen.findByRole('button', { name: 'Flatten & Stop' }));
    expect(screen.getByText('This will close all open positions.')).toBeTruthy();
  });

  it('renders the backend-selected recovery-primary action once, in the banner, not the accordion (#1665)', async () => {
    const fakeSvc = makeFakePanelService();
    const actionRequested = vi.fn();
    const recoveryAction: PanelAction = {
      action_id: 'recover_exact_execution_evidence',
      label: 'Recover exact execution evidence',
      explanation: 'Read one retained Alpaca paper execution.',
      enabled: true,
      blockers: [],
      confirmation: null,
      revision: 1,
      concurrency_token: 'historical-token',
    };
    const panel: BotPanelView = {
      ...makePanel(),
      actions: [recoveryAction],
      // The backend policy folds RecoveryCapability.primary into the Operator
      // reference (ADR 0027 precedence); readiness must suppress exactly this
      // operation's accordion row rather than re-deriving its own promotion.
      primary_action_by_lens: { trader: null, operator: 'recover_exact_execution_evidence' },
      readiness_checks: [makeReadinessCheck(recoveryAction, {
        evidence: { primary: true },
        cure: 'Prepare the exact paper execution evidence.',
      })],
      readiness_ready_count: 1,
    };

    await render(OperatorLensComponent, {
      inputs: {
        panel,
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      on: { actionRequested },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expect(screen.getAllByRole('button', { name: recoveryAction.label })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: recoveryAction.label }));

    expect(actionRequested).toHaveBeenCalledWith({ action: recoveryAction, reason: null });
  });

  it('keeps a disabled operator action reason code visible with its current gate', async () => {
    const fakeSvc = makeFakePanelService();
    const blockedExplanation = 'The bot is already stopped with no attributed exposure.';
    const flattenAction: PanelAction = {
      action_id: 'flatten_stop',
      label: 'Flatten & stop',
      explanation: blockedExplanation,
      enabled: false,
      blockers: [
        {
          condition: {
            id: 'BOT_ALREADY_STOPPED_FLAT',
            severity: 'blocking',
            scope: 'bot',
          },
          host: 'bot_cockpit',
          anchor: { kind: 'surface', subject_key: null },
          audience: 'both',
          disposition: 'terminal',
          headline: blockedExplanation,
          detail: 'No flatten command is necessary.',
          primary_move: null,
          secondary_moves: [],
          applies_to: 'run',
        },
        {
          condition: {
            id: 'ACCOUNT_CUSTODY_UNPROVABLE',
            severity: 'blocking',
            scope: 'account',
          },
          host: 'bot_cockpit',
          anchor: { kind: 'surface', subject_key: null },
          audience: 'both',
          disposition: 'fix_elsewhere',
          headline: 'The Clerk cannot prove the exposure to flatten.',
          detail: 'Restore broker observation and run Reconcile now before flattening.',
          primary_move: null,
          secondary_moves: [],
          applies_to: 'run',
        },
      ],
      confirmation: null,
      revision: 1,
      concurrency_token: 'test-token',
    };
    const panel: BotPanelView = {
      ...makePanel(),
      actions: [flattenAction],
      readiness_checks: [
        makeReadinessCheck(flattenAction, {
          explanation: blockedExplanation,
          cure: 'No flatten command is necessary.',
        }),
      ],
      readiness_blocked_count: 1,
    };

    await render(OperatorLensComponent, {
      inputs: {
        panel,
        profile: makeProfile(),
        actionPending: false,
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: fakeSvc }],
    });

    expect(screen.getByText(/Active command gates/)).toBeTruthy();
    expect(
      screen.queryByText(
        'These checks gate commands now. They are not historical transaction stages.',
      ),
    ).toBeNull();
    expect(screen.getAllByText(blockedExplanation)).toHaveLength(1);
    expect(
      screen.getAllByText(/No flatten command is necessary\./),
    ).toHaveLength(1);

    const disclosure = screen.getByRole('button', { name: /Blocked Flatten & stop/ });
    expect(disclosure.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(disclosure);
    expect(disclosure.getAttribute('aria-expanded')).toBe('true');

    expect(await screen.findByRole('button', { name: 'Flatten & stop' })).toBeTruthy();
    expect(
      screen.getAllByRole('alert').some((alert) =>
        alert.textContent?.includes('Bot Already Stopped Flat'),
      ),
    ).toBe(true);
    expect(screen.queryByLabelText('Operator commands')).toBeNull();
  });
});
