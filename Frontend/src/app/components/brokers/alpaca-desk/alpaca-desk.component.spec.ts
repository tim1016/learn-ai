import { fireEvent, render, screen } from '@testing-library/angular';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { of } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AlpacaDeskState } from '../../../api/alpaca.types';
import type { DeskLens } from '../../../shared/lens/lens';
import { ActiveLensBridgeService } from '../../../shared/lens/active-lens-bridge.service';
import { BrokersService } from '../../../services/brokers.service';
import { healthyAccountOperatorPostureFixture } from '../../../testing/operator-blocker-fixtures';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { BrokerConfigurationService } from './configuration/broker-configuration.service';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';
import { AlpacaDeskComponent } from './alpaca-desk.component';
import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';
import { CurrentUrlService } from '../../../shell/current-url.service';

// The tab UI moved to the global top bar (ActiveLensBridgeService); this
// mirrors what it does — call the registered host's own select() — so
// switching still runs the desk's own persistence and side effects. The
// registration itself only lands once an operating account is visible.
async function switchLens(
  fixture: ComponentFixture<AlpacaDeskComponent>,
  lens: DeskLens,
): Promise<void> {
  const bridge = TestBed.inject(ActiveLensBridgeService);
  await vi.waitFor(() => expect(bridge.host()).not.toBeNull());
  bridge.host()?.select(lens);
  await fixture.whenStable();
  fixture.detectChanges();
}

const LENS_STORAGE_KEY = 'learn-ai.alpaca-desk.lens';

const accountActivation: AlpacaDeskState = {
  activation_state: 'no_selection',
  headline: 'No Alpaca account is active',
  detail: 'Choose an approved account to prepare this desk.',
  lifecycle: [
    {
      key: 'effective_configuration',
      label: 'No active account',
      status: 'current',
      status_label: 'Current step',
    },
    {
      key: 'selected_configuration',
      label: 'Select configuration',
      status: 'pending',
      status_label: 'Pending',
    },
    {
      key: 'worker_handoff',
      label: 'Restart worker',
      status: 'pending',
      status_label: 'Pending',
    },
  ],
  selection_label: 'Approved accounts',
  empty_choices_message: 'No approved accounts are available. Verify one in Configuration.',
  consequence: 'Reviewing a choice does not change the running worker.',
  action: {
    kind: 'review_configuration',
    label: 'Review selected account',
    enabled: true,
  },
  selection_generation: 0,
  staged_choice: null,
  effective_choice: null,
  choices: [{
    selection_id: 'paper-profile:3',
    profile_id: 'paper-profile',
    revision: 3,
    profile_label: 'Alpaca Paper',
    account_id: 'PA-123',
    nickname: 'Strategy lab',
    account_label: 'Strategy lab · PA-123',
    endpoint_mode: 'paper',
    badge_label: 'Paper',
    description: 'For strategy testing with no live capital.',
    is_staged: false,
    is_effective: false,
    action_kind: 'review_configuration',
    action_label: 'Select Paper account',
    action_consequence: 'Review this saved revision without changing the running worker.',
  }],
  profiles_requiring_setup: 0,
  setup_required_message: null,
  restart_command: null,
};

const effectiveSelection: AlpacaDeskState = {
  ...accountActivation,
  activation_state: 'effective_selection',
  headline: 'Strategy lab is the effective selection',
  detail: 'The worker last acknowledged Alpaca Paper.',
  effective_choice: accountActivation.choices[0],
  action: { kind: 'review_configuration', label: 'Review account configuration', enabled: true },
};

function brokerService() {
  return {
    getAccount: vi.fn().mockResolvedValue({
      broker: 'alpaca',
      account_id: 'PA1',
      account_mode: 'paper',
      account_status: 'ACTIVE',
      currency: 'USD',
      cash: 1_000,
      equity: 1_000,
      buying_power: 2_000,
      portfolio_value: 1_000,
      long_market_value: 0,
      short_market_value: 0,
      pattern_day_trader: false,
      trading_blocked: false,
      account_blocked: false,
      created_at_ms: null,
      observed_at_ms: 1,
    }),
    listPositions: vi.fn().mockResolvedValue([]),
    listActivities: vi.fn().mockResolvedValue([]),
    getPortfolioHistory: vi.fn().mockResolvedValue({
      timestamps: [1, 2], equity: [1_000, 1_000], profit_loss: [0, 0],
      base_value: 1_000, timeframe: '1D',
    }),
    getClerkStatus: vi.fn().mockResolvedValue({
      broker: 'alpaca',
      account_id: 'PA1',
      hold: { active: false, reason_code: null, reason: null, since_ms: null },
      latest_reconciliation: null,
      outstanding_intents: 0,
      observed_at_ms: 1,
      operator_posture: healthyAccountOperatorPostureFixture(),
    }),
    getCustodyDiagnosis: vi.fn().mockResolvedValue({
      broker: 'alpaca',
      account_id: 'PA1',
      in_sync: true,
      observed_at_ms: 1,
      snapshot_version: 'v1',
      resolution_posture: 'paper',
      resolvable: false,
      blocked_reason: null,
      divergences: [],
      resolution_plan: [],
    }),
    getSqliteClerkProjection: vi.fn().mockResolvedValue({
      account_id: 'PA1', strategy_instance_id: null, authority_generation: 1,
      db_identity_token: 'db-1', authority_health: 'healthy', authority_health_reason: null,
      control_revision: 1, custody_owner: 'ACCOUNT_CLERK', runs: [], commands: [],
      operations: [], positions: [], holds: [], uncertainties: [], latest_reconciliation: null,
      terminal_receipts: [], recovery_actions: [], generated_at_ms: 1,
      guidance: {
        headline: 'Account record is healthy', explanation: 'No unresolved account uncertainty.',
        scope: 'ACCOUNT_CLERK', impact: 'Normal controls remain available.',
        custody_owner: 'ACCOUNT_CLERK', may_create_exposure: true,
        available_safety_actions: [], action_required: false,
        next_step: 'No recovery action is required.',
      },
    }),
    getSqliteManualOrderCapability: vi.fn().mockResolvedValue({
      available: false,
      unavailable: {
        code: 'MANUAL_TRADING_NOT_QUALIFIED',
        message: 'Manual SQLite trading remains disabled until paper qualification is complete.',
      },
      supported_order_shape: 'BUY market DAY equity, one leg',
    }),
    getSqliteManualOrderTicket: vi.fn().mockRejectedValue(new HttpErrorResponse({ status: 404 })),
    previewSqliteManualOrder: vi.fn(),
    submitSqliteManualOrder: vi.fn(),
  };
}

function manualTicketQuery(overrides: Record<string, string> = {}): Record<string, string> {
  return {
    order: 'new',
    accountId: 'PA1',
    symbol: 'SPY',
    ticketId: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
    legId: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
    ...overrides,
  };
}

async function renderDesk(
  query: Record<string, string> = {},
  brokers = brokerService(),
  deskState: AlpacaDeskState = accountActivation,
  fleetDirectory = provideFleetDirectory(),
) {
  const queryParamMap = convertToParamMap(query);
  const paramMap = convertToParamMap({ clerkId: 'clrk_spec0000000000000000aa', accountId: 'PA1' });
  const view = await render(AlpacaDeskComponent, {
    providers: [
      fleetDirectory,
      provideRouter([]),
      // Provided by the account workspace in the app; the Overview tab is
      // rendered alone here, so the spec stands in for that parent.
      AlpacaDeskAccountDataService,
      {
        provide: ActivatedRoute,
        useValue: {
          queryParamMap: of(queryParamMap),
          paramMap: of(paramMap),
          snapshot: { queryParamMap, paramMap },
        },
      },
      // AlpacaDeskAccountDataService reads its routed accountId from the URL
      // (accountWorkspaceLocation), not from this ActivatedRoute fake — see
      // that service's own spec for why. Match the paramMap above.
      {
        provide: CurrentUrlService,
        useValue: { url: () => '/brokers/alpaca/clerks/clrk_spec0000000000000000aa/accounts/PA1' },
      },
      {
        provide: BrokersService,
        useValue: {
          ...brokers,
          accountTransactions: vi.fn().mockResolvedValue({
            projection_available: true, canonical_fallback_required: false, feed_state: 'live',
            feed_headline: 'Current', feed_detail: 'Current', high_water_journal_seq: 0,
            lag_records: 0, lag_is_lower_bound: false,
            custody_summary: {
              record_count: 0, a0_custody_accepted_count: 0,
              a1_broker_write_started_count: 0, a2_broker_known_count: 0,
              a3_economic_terminal_count: 0, uncertain_count: 0,
            },
            rows: [], next_cursor: null,
          }),
          accountTransaction: vi.fn(),
        },
      },
      {
        provide: BrokerV2PanelService,
        useValue: { getDeployView: () => new Promise<never>(() => undefined) },
      },
      {
        provide: BrokerConfigurationService,
        useValue: { readDeskState: vi.fn().mockResolvedValue(deskState) },
      },
    ],
  });

  return { brokers, router: view.fixture.debugElement.injector.get(Router), view };
}

describe('AlpacaDeskComponent', () => {
  beforeEach(() => localStorage.clear());

  it('defaults to the Trader lens while restoring account safety surfaces', async () => {
    const { brokers } = await renderDesk();

    expect(await screen.findByRole('heading', { name: 'Trader desk' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Operator desk' })).toBeNull();
    // #2185: the account name, its mode, equity and Deploy are the account
    // workspace's header — the Overview tab renders none of them itself.
    expect(screen.queryByRole('button', { name: 'Deploy strategy' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Alpaca clerk lanes' })).toBeNull();
    expect(await screen.findByLabelText('Clerk and broker in sync')).toBeTruthy();
    expect(brokers.getClerkStatus).toHaveBeenCalledOnce();
    expect(brokers.getSqliteClerkProjection).not.toHaveBeenCalled();

    // #2188: the account number guards nothing on a tab the header already
    // names the account on. It used to reach this page twice — the identity
    // strip's observed-account fallback and the summary card.
    await screen.findByLabelText('Alpaca account summary');
    expect(screen.queryAllByText('PA1')).toHaveLength(0);
  });

  it('switches instantly, updates the query parameter, persists, and lazy-loads operator data', async () => {
    const { brokers, router, view } = await renderDesk();
    // Wait for the account read to land before switching lenses. Keyed on the
    // card's own landmark rather than the account number, which the desk no
    // longer prints anywhere (#2188).
    await screen.findByLabelText('Alpaca account summary');

    await switchLens(view.fixture, 'operator');

    expect(screen.getByRole('heading', { name: 'Operator desk' })).toBeTruthy();
    expect(localStorage.getItem(LENS_STORAGE_KEY)).toBe('operator');
    await vi.waitFor(() => expect(router.url).toContain('lens=operator'));
    await vi.waitFor(() => expect(brokers.getClerkStatus).toHaveBeenCalledTimes(2));
    expect(brokers.getAccount).toHaveBeenCalledOnce();

    await switchLens(view.fixture, 'trader');
    await switchLens(view.fixture, 'operator');

    expect(brokers.getClerkStatus).toHaveBeenCalledTimes(2);
    expect(brokers.getAccount).toHaveBeenCalledOnce();
  });

  it('opens the Operator lens from a query deep link', async () => {
    const { brokers } = await renderDesk({ lens: 'operator' });

    expect(await screen.findByRole('heading', { name: 'Operator desk' })).toBeTruthy();
    await vi.waitFor(() => expect(brokers.getClerkStatus).toHaveBeenCalledTimes(2));
  });

  it('offers account activation without rendering operating controls when no account is effective', async () => {
    const brokers = brokerService();
    brokers.getAccount.mockRejectedValue(new HttpErrorResponse({ status: 409 }));
    const { router } = await renderDesk({}, brokers, accountActivation);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    expect(await screen.findByRole('heading', { name: accountActivation.headline })).toBeTruthy();
    expect(screen.queryByRole('tab', { name: 'Trader' })).toBeNull();
    expect(screen.queryByRole('tab', { name: 'Operator' })).toBeNull();

    await fireEvent.click(screen.getByRole('radio', { name: /Strategy lab · PA-123/ }));
    await fireEvent.click(screen.getByRole('button', { name: 'Select Paper account' }));

    expect(navigate).toHaveBeenCalledWith(['/brokers', 'alpaca', 'clerks', 'clrk_spec0000000000000000aa', 'configuration'], {
      queryParams: { profileId: 'paper-profile', revision: 3 },
    });
  });

  it('keeps the operating desk visible when account data succeeds during bootstrap', async () => {
    await renderDesk({}, brokerService(), accountActivation);

    expect(await screen.findByRole('heading', { name: 'Trader desk' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: accountActivation.headline })).toBeNull();
  });

  it('keeps the connectivity failure distinct when activation guidance is unavailable', async () => {
    const brokers = brokerService();
    brokers.getAccount.mockRejectedValue(new HttpErrorResponse({ status: 503 }));

    await renderDesk({}, brokers, effectiveSelection);
    const effective = effectiveSelection.effective_choice;
    if (effective === null) throw new Error('effective selection fixture is incomplete');

    expect(await screen.findByText(/Couldn't reach Alpaca/)).toBeTruthy();
    // The identity strip and the connectivity section both name the effective profile.
    expect((await screen.findAllByText(effective.profile_label)).length).toBeGreaterThan(0);
    expect(screen.getByText(/Strategy lab · PA-123.*Paper.*Revision 3/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Review account configuration' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: accountActivation.headline })).toBeNull();
  });

  it('fails closed when the Account Clerk returns a different account than the route', async () => {
    const brokers = brokerService();
    brokers.getAccount.mockResolvedValue({
      ...(await brokers.getAccount()),
      account_id: 'PA-OTHER',
    });

    await renderDesk({}, brokers, effectiveSelection);

    expect(await screen.findByText(/Couldn't reach Alpaca/)).toBeTruthy();
    expect(screen.queryByRole('tab', { name: 'Trader' })).toBeNull();
    expect(screen.queryByText('PA-OTHER')).toBeNull();
  });

  it('keeps a connected account usable while showing the exact pending configuration', async () => {
    const stagedChoice = {
      ...accountActivation.choices[0],
      selection_id: 'live-profile:1',
      profile_id: 'live-profile',
      revision: 1,
      profile_label: 'Alpaca Live',
      account_id: 'LIVE-1',
      nickname: 'Live account',
      account_label: 'Live account',
      endpoint_mode: 'live' as const,
      badge_label: 'Live',
      description: 'Selecting this profile does not arm live trading.',
      action_label: 'Review Alpaca Live',
    };
    const pending: AlpacaDeskState = {
      ...accountActivation,
      activation_state: 'staged_not_applied',
      headline: 'Strategy lab remains the effective selection',
      detail: 'Alpaca Live is selected next; the effective configuration remains Alpaca Paper.',
      staged_choice: stagedChoice,
      // The connected account must match the effective choice, or the desk's
      // identity-mismatch gating correctly disables the pending action.
      effective_choice: { ...accountActivation.choices[0], account_id: 'PA1' },
      action: { kind: 'review_staged_configuration', label: 'Review & apply Alpaca Live', enabled: true },
    };
    const { router } = await renderDesk({}, brokerService(), pending);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    expect(await screen.findByText(pending.headline)).toBeTruthy();
    expect(screen.getByText(pending.consequence)).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Trader desk' })).toBeTruthy();

    await fireEvent.click(screen.getByRole('button', { name: pending.action.label }));
    expect(navigate).toHaveBeenCalledWith(['/brokers', 'alpaca', 'clerks', 'clrk_spec0000000000000000aa', 'configuration'], {
      queryParams: { profileId: 'live-profile', revision: 1 },
    });
  });

  it('blocks a matching manual-order deep link when SQLite authority is unavailable', async () => {
    const brokers = brokerService();
    brokers.getSqliteManualOrderCapability.mockRejectedValue(
      new HttpErrorResponse({ status: 409 }),
    );

    await renderDesk(manualTicketQuery({ symbol: 'spy' }), brokers);

    expect(await screen.findByText(/SQLite order authority is unavailable/)).toBeTruthy();
    expect(screen.queryByText('Create Alpaca order')).toBeNull();
  });

  it('refuses a manual-order link for a different account', async () => {
    const { brokers } = await renderDesk(manualTicketQuery({ accountId: 'PA-OTHER' }));

    expect((await screen.findByRole('alert')).textContent).toContain(
      'The order link targets account PA-OTHER, but Alpaca is connected to PA1. No ticket was opened.',
    );
    expect(screen.queryByRole('heading', { name: 'Create Alpaca order' })).toBeNull();
    expect(brokers.getSqliteManualOrderCapability).not.toHaveBeenCalled();
  });

  it('opens a SQLite ticket with the server-owned disabled reason', async () => {
    const { brokers } = await renderDesk(manualTicketQuery());

    expect(
      await screen.findByText(/Manual SQLite trading remains disabled until paper qualification is complete/),
    ).toBeTruthy();
    expect(await screen.findByText('Create Alpaca order')).toBeTruthy();
    expect(brokers.getSqliteManualOrderCapability).toHaveBeenCalledWith('clrk_spec0000000000000000aa', 'PA1');
  });

  it('restores the last selected lens when no query parameter is present', async () => {
    localStorage.setItem(LENS_STORAGE_KEY, 'operator');

    await renderDesk();

    expect(await screen.findByRole('heading', { name: 'Operator desk' })).toBeTruthy();
  });

  it('registers with the global lens toggle once an operating account is visible, with the alpaca id prefix', async () => {
    await renderDesk();
    await screen.findByLabelText('Alpaca account summary');

    const host = TestBed.inject(ActiveLensBridgeService).host();
    if (host === null) throw new Error('Expected the desk to register as the active lens host.');
    expect(host.lens()).toBe('trader');
    expect(host.idPrefix).toBe('alpaca');
  });
});
