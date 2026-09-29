import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import type {
  BotPanelView,
  ChannelHealthView,
  EvidencePage,
  PanelAction,
  PanelProfile,
  ReadinessCheckView,
} from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { fakeBotPanelView, fakePanelAction, fakeSqliteStopAction } from '../../../../testing/bot-panel-fixtures';
import { provideFleetDirectory, TEST_CLERK_ID } from '../../../../fleet/fleet-directory-testing';
import { BotDetailsComponent } from './bot-details.component';

/** A real-looking broker account number, so "never rendered" is a meaningful claim. */
const ACCOUNT_NUMBER = '318420190';
type SealedProgram = NonNullable<BotPanelView['sealed_program']>;
const OBSERVED_AT_MS = 1_700_000_001_000;

const PROFILE: PanelProfile = {
  broker: 'alpaca',
  fee_fidelity: 'none',
  live_bars_supported: true,
  stations: [],
  supported_action_ids: [],
};

function channel(overrides: Partial<ChannelHealthView>): ChannelHealthView {
  return {
    stream: 'market_data',
    name: 'IBKR market data',
    state: 'healthy',
    label: 'Healthy',
    explanation: 'The channel is connected and current.',
    reason: 'Current IBKR bars are arriving.',
    observed_at_ms: OBSERVED_AT_MS,
    ...overrides,
  };
}

function check(action: PanelAction, overrides: Partial<ReadinessCheckView> = {}): ReadinessCheckView {
  return {
    operation: action.action_id,
    label: action.label,
    ready: action.enabled,
    scope: 'bot',
    authority: 'SQLite Account Clerk recovery policy',
    explanation: action.explanation,
    evidence: {},
    evaluated_at_ms: OBSERVED_AT_MS,
    cure: null,
    ...overrides,
  };
}

function evidencePage(): EvidencePage {
  return {
    strategy_instance_id: 'spy-momentum-01',
    account_id: ACCOUNT_NUMBER,
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
    read_at_ms: OBSERVED_AT_MS,
  };
}

/** A running bot with deployed exit terms, a seal, two checks and two channels. */
function panelView(overrides: Partial<BotPanelView> = {}): BotPanelView {
  const base = fakeBotPanelView();
  const stop = fakeSqliteStopAction();
  const reconcile = fakePanelAction('reconcile_now', {
    label: 'Reconcile now',
    explanation: 'Refresh the account’s order records.',
    enabled: false,
  });
  return {
    ...base,
    account_id: ACCOUNT_NUMBER,
    exit_terms: {
      exit_allowance_bps: 25,
      band_multiple: 2,
      spread_cap_bps: 50,
      provenance: 'deployed',
    },
    actions: [stop, reconcile],
    readiness_checks: [check(stop), check(reconcile)],
    readiness_ready_count: 1,
    readiness_blocked_count: 1,
    clerk: {
      ...base.clerk,
      account_id: ACCOUNT_NUMBER,
      channels: [
        channel({}),
        channel({
          stream: 'execution',
          name: 'Alpaca execution',
          state: 'unhealthy',
          label: 'Down',
          explanation: 'Order updates from Alpaca stopped arriving.',
        }),
      ],
    },
    sealed_program: {
      action_plan: {},
      bot_configuration_hash: 'sha256:configuration0001',
      broker: 'alpaca',
      carryover_policy: 'FORBID',
      // The fold never reads the signal contract; a full one would be noise here.
      configured_signal: {} as SealedProgram['configured_signal'],
      configured_signal_hash: 'sha256:signal0002',
      mode: 'trade',
      quantity: 1,
      sealed_account_id: ACCOUNT_NUMBER,
      sealed_at_ms: OBSERVED_AT_MS,
      strategy_instance_id: 'spy-momentum-01',
      validation_event_id: 'val-evt-0003',
      validation_snapshot_sha256: 'sha256:snapshot0004',
    },
    working_orders: [
      {
        broker_order_id: 'b-1',
        filled_quantity: null,
        observed_at_ms: OBSERVED_AT_MS,
        order_ref: 'ord-7f3c-a91',
        quantity: 1,
        side: 'sell',
        status: 'accepted',
        symbol: 'SPY',
      },
    ],
    ...overrides,
  };
}

function fakePanelService(getEvidence = vi.fn(() => Promise.resolve(evidencePage()))) {
  return {
    getEvidence,
  };
}

async function renderDetails(
  panel: BotPanelView = panelView(),
  service = fakePanelService(),
) {
  const actionRequested = vi.fn();
  const transactionSelected = vi.fn();
  const view = await render(
    `<main aria-label="Bot">
      <app-bot-details
        [panel]="panel"
        [profile]="profile"
        broker="alpaca"
        [clerkId]="clerkId"
        [accountId]="accountId"
        sid="spy-momentum-01"
        (actionRequested)="actionRequested($event)"
        (transactionSelected)="transactionSelected($event)"
      />
    </main>`,
    {
      imports: [BotDetailsComponent],
      componentProperties: {
        panel,
        profile: PROFILE,
        clerkId: TEST_CLERK_ID,
        accountId: ACCOUNT_NUMBER,
        actionRequested,
        transactionSelected,
      },
      providers: [
        provideFleetDirectory(),
        { provide: BrokerV2PanelService, useValue: service },
      ],
    },
  );
  return { ...view, service, actionRequested, transactionSelected };
}

function fold(title: string): HTMLDetailsElement {
  const details = screen.getByText(title, { selector: 'summary > b' }).closest('details');
  if (details === null) throw new Error(`Expected a "${title}" fold.`);
  return details;
}

function setFold(title: string, open: boolean): HTMLDetailsElement {
  const details = fold(title);
  details.open = open;
  fireEvent(details, new Event('toggle'));
  return details;
}

function summaryText(title: string): string {
  return fold(title).querySelector('summary')?.textContent?.replace(/\s+/g, ' ').trim() ?? '';
}

const FOLDS = ['Exit terms', 'Checks', 'Connections', 'Order records', 'Run evidence'];

describe('BotDetailsComponent', () => {
  it('renders the five folds under one Details heading, all closed', async () => {
    await renderDetails();

    const section = screen.getByRole('region', { name: 'Details' });
    expect(within(section).getByRole('heading', { level: 3, name: 'Details' })).toBeTruthy();
    const folds = Array.from(section.querySelectorAll('details'))
      .filter((details) => details.parentElement === section);
    expect(folds.map((details) => details.querySelector('summary > b')?.textContent)).toEqual(FOLDS);
    expect(folds.every((details) => !details.open)).toBe(true);
    expect(summaryText('Exit terms')).toBe('Exit terms · fixed for this bot');
    expect(summaryText('Checks')).toBe('Checks · 1 of 2 pass');
    expect(summaryText('Connections')).toBe('Connections · 1 needs attention');
    expect(summaryText('Order records')).toBe('Order records · 1 working order');
    expect(summaryText('Run evidence')).toBe('Run evidence · On duty');
  });

  it('shows deployed exit terms in owner units', async () => {
    await renderDetails();

    const terms = within(fold('Exit terms'));
    expect(terms.getByText('Exit allowance').nextElementSibling?.textContent?.trim()).toBe('25 bps');
    expect(terms.getByText('Band multiple').nextElementSibling?.textContent?.trim()).toBe('2×');
    expect(terms.getByText('Spread cap').nextElementSibling?.textContent?.trim()).toBe('50 bps');
    expect(terms.queryByText(/filled in for a bot deployed before/)).toBeNull();
  });

  it('says when exit terms were filled in for an older bot, and when the allowance is unset', async () => {
    await renderDetails(panelView({
      exit_terms: { exit_allowance_bps: null, band_multiple: 1.5, spread_cap_bps: 40, provenance: 'backfilled' },
    }));

    const terms = within(fold('Exit terms'));
    expect(terms.getByText('Exit allowance').nextElementSibling?.textContent?.trim()).toBe('Not set');
    expect(terms.getByText('Band multiple').nextElementSibling?.textContent?.trim()).toBe('1.5×');
    expect(
      terms.getByText('These terms were filled in for a bot deployed before it recorded its own.'),
    ).toBeTruthy();
  });

  it('says so when a bot recorded no exit terms', async () => {
    await renderDetails(panelView({ exit_terms: null }));

    expect(summaryText('Exit terms')).toBe('Exit terms · none recorded');
    expect(within(fold('Exit terms')).getByText('This bot recorded no exit terms.')).toBeTruthy();
  });

  it('lists every check with its state, and keeps the header action out of the list', async () => {
    const { actionRequested } = await renderDetails(panelView({ primary_action: 'stop_bot_decisions' }));
    setFold('Checks', true);

    const checks = within(fold('Checks'));
    const stopRow = checks.getByRole('button', { name: /Ready Stop bot decisions/ });
    fireEvent.click(stopRow);
    expect(checks.getByText('Stop the bot making new decisions. Stopping doesn\'t sell its shares.')).toBeTruthy();
    expect(checks.queryByRole('button', { name: 'Stop bot decisions' })).toBeNull();
    expect(checks.getByRole('button', { name: /Blocked Reconcile now/ })).toBeTruthy();
    expect(actionRequested).not.toHaveBeenCalled();
  });

  it('shows each connection with its dot and its state in words, never the account number', async () => {
    const { container } = await renderDetails();
    setFold('Connections', true);

    const connections = within(fold('Connections'));
    const items = connections.getAllByRole('listitem').filter((item) => item.classList.contains('channel'));
    expect(items).toHaveLength(2);
    for (const item of items) {
      const dot = item.querySelector('app-channel-health-dot');
      expect(dot?.getAttribute('aria-hidden')).toBe('true');
    }
    expect(within(items[0]).getByText('IBKR market data')).toBeTruthy();
    expect(within(items[0]).getByText('Healthy')).toBeTruthy();
    expect(within(items[0]).getByText('Continuous')).toBeTruthy();
    expect(within(items[1]).getByText('Alpaca execution')).toBeTruthy();
    expect(within(items[1]).getByText('Down')).toBeTruthy();
    expect(within(items[1]).getByText('Order updates from Alpaca stopped arriving.')).toBeTruthy();
    expect(connections.getByText('Market data live')).toBeTruthy();
    expect(connections.getByText('Orders in doubt')).toBeTruthy();
    expect(connections.queryByRole('button')).toBeNull();

    for (const title of FOLDS) setFold(title, true);
    expect(container.textContent).not.toContain(ACCOUNT_NUMBER);
  });

  it('reports every connection healthy as all connected', async () => {
    const panel = panelView();
    await renderDetails(panelView({
      clerk: { ...panel.clerk, channels: panel.clerk.channels.map((c) => ({ ...c, state: 'healthy' })) },
    }));

    expect(summaryText('Connections')).toBe('Connections · all connected');
  });

  it('reads the audit trail only once Order records is first opened', async () => {
    const { service, fixture } = await renderDetails();

    for (const title of FOLDS.filter((t) => t !== 'Order records')) setFold(title, true);
    await fixture.whenStable();
    expect(service.getEvidence).not.toHaveBeenCalled();

    setFold('Order records', true);
    await fixture.whenStable();
    expect(service.getEvidence).toHaveBeenCalledTimes(1);
    expect(service.getEvidence).toHaveBeenCalledWith(
      expect.objectContaining({ broker: 'alpaca', clerkId: TEST_CLERK_ID, accountId: ACCOUNT_NUMBER }),
      'spy-momentum-01',
      { pageSize: 24, clientHint: 'bot-page-order-records' },
    );
    expect(await screen.findByText('BUY 10 SPY @ market')).toBeTruthy();

    setFold('Order records', false);
    setFold('Order records', true);
    await fixture.whenStable();
    expect(service.getEvidence).toHaveBeenCalledTimes(1);
  });

  it('passes an audit-trail selection up to the page', async () => {
    const { fixture, transactionSelected } = await renderDetails();
    setFold('Order records', true);
    await fixture.whenStable();

    fireEvent.click(await screen.findByRole('button', { name: 'Select transaction tx-001 on rail' }));

    expect(transactionSelected).toHaveBeenCalledWith('tx-001');
  });

  it('keeps every other fold working when the audit trail cannot be read', async () => {
    const failing = vi.fn(() => Promise.reject(new Error('evidence read failed')));
    const { fixture } = await renderDetails(panelView(), fakePanelService(failing));
    setFold('Order records', true);
    await fixture.whenStable();

    expect(await screen.findByText('Could not load the audit trail.')).toBeTruthy();
    const records = within(fold('Order records'));
    expect(records.getByText('ord-7f3c-a91')).toBeTruthy();
    expect(within(fold('Exit terms')).getByText('25 bps')).toBeTruthy();
  });

  it('shows the sealed program with its hashes verbatim', async () => {
    await renderDetails();

    const evidence = within(fold('Run evidence'));
    for (const hash of ['sha256:configuration0001', 'sha256:signal0002', 'sha256:snapshot0004']) {
      expect(evidence.getByText(hash).tagName).toBe('CODE');
    }
    expect(evidence.getByText('deployment_validation').tagName).toBe('CODE');
    expect(evidence.getByText('No Signal Program build proof supplied.')).toBeTruthy();
  });

  it('says so when no program was sealed', async () => {
    await renderDetails(panelView({ sealed_program: null }));

    expect(
      within(fold('Run evidence')).getByText('No sealed program was recorded for this bot.'),
    ).toBeTruthy();
  });

  it('passes AXE with every fold open', async () => {
    const { fixture } = await renderDetails();
    for (const title of FOLDS) setFold(title, true);
    await fixture.whenStable();
    await screen.findByText('BUY 10 SPY @ market');

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
