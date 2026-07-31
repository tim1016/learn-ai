import { render, screen, fireEvent } from '@testing-library/angular';
import { describe, it, expect, vi } from 'vitest';
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
    updated_at_ms: 1_700_000_001_000,
    revision: 1,
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
    readiness_checks: [],
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
  };
}

// ── Tests ──────────────────────────────────────────────────────────────────────

describe('OperatorLensComponent', () => {
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

    expect(screen.getByText('Live')).toBeTruthy();
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

  it('calls getEvidence on mount to load the journal tail', async () => {
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

    expect(fakeSvc.getEvidence).toHaveBeenCalledWith(
      'alpaca',
      'acc-1',
      'sid-1',
      expect.objectContaining({ clientHint: 'operator-lens-journal-tail' }),
    );
  });

  it('journal tail shows loaded entries', async () => {
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

    // The journal tail renders the kind_label from the entry.
    const matches = await screen.findAllByText('Order submitted');
    expect(matches.length).toBeGreaterThan(0);
  });

  it('flatten-stop button is present when the action is in the panel', async () => {
    const fakeSvc = makeFakePanelService();
    const panel: BotPanelView = {
      ...makePanel(),
      actions: [
        {
          action_id: 'flatten_stop',
          label: 'Flatten & Stop',
          explanation: 'Flattens all positions and stops the bot.',
          enabled: true,
          blockers: [],
          confirmation: null,
          revision: 1,
          concurrency_token: 'test-token',
        },
      ],
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

    expect(screen.getByRole('button', { name: /flatten & stop/i })).toBeTruthy();
  });

  it('flatten-stop button is disabled when action is disabled', async () => {
    const fakeSvc = makeFakePanelService();
    const panel: BotPanelView = {
      ...makePanel(),
      actions: [
        {
          action_id: 'flatten_stop',
          label: 'Flatten & Stop',
          explanation: 'Not available.',
          enabled: false,
          blockers: [],
          confirmation: null,
          revision: 1,
          concurrency_token: 'test-token',
        },
      ],
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

    const btn = screen.getByRole('button', { name: /flatten & stop/i });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });

  it('clicking flatten-stop calls actionRequested with the action', async () => {
    const fakeSvc = makeFakePanelService();
    const onActionRequested = vi.fn() as unknown as (action: PanelAction) => void;
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

    fireEvent.click(screen.getByRole('button', { name: /flatten & stop/i }));
    await fixture.whenStable();

    expect(onActionRequested).toHaveBeenCalledWith(flattenAction);
  });

  it('evidence drawer is hidden by default', async () => {
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

    // The evidence drawer with backdrop renders only when open.
    const backdrop = container.querySelector('.evidence-drawer__backdrop');
    expect(backdrop).toBeNull();
  });

  it('shows confirmation prompt when flatten-stop has confirmation.required', async () => {
    const fakeSvc = makeFakePanelService();
    const panel: BotPanelView = {
      ...makePanel(),
      actions: [
        {
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
        },
      ],
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

    fireEvent.click(screen.getByRole('button', { name: 'Flatten & Stop' }));
    expect(screen.getByText('This will close all open positions.')).toBeTruthy();
  });

  it('shows the inventory recovery action with typed confirmation', async () => {
    const fakeSvc = makeFakePanelService();
    const panel: BotPanelView = {
      ...makePanel(),
      actions: [
        {
          action_id: 'record_inventory_baseline',
          label: 'Recover inventory baseline',
          explanation: 'Record a verified accounting cutover.',
          enabled: true,
          blockers: [],
          confirmation: {
            title: 'Adopt current broker inventory?',
            body: 'Reads the current Alpaca positions.',
            consequence: 'Earlier trades remain in audit history.',
            confirm_label: 'Recover inventory baseline',
            required_token: 'BASELINE',
          },
          revision: 1,
          concurrency_token: 'baseline-token',
        },
      ],
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

    fireEvent.click(
      screen.getByRole('button', { name: 'Recover inventory baseline' }),
    );
    expect(screen.getByText('Reads the current Alpaca positions.')).toBeTruthy();
    expect(screen.getByText('BASELINE')).toBeTruthy();
  });
});
