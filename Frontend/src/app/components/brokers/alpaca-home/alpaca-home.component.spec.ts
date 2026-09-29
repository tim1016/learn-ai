import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import {
  Router,
  RouterOutlet,
  provideRouter,
  withComponentInputBinding,
  withRouterConfig,
  type Routes,
} from '@angular/router';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { MessageService } from 'primeng/api';
import { describe, expect, it, vi } from 'vitest';

import {
  TEST_ACCOUNT_ID,
  TEST_CLERK_ID,
  provideFleetDirectory,
} from '../../../fleet/fleet-directory-testing';
import { BrokersService } from '../../../services/brokers.service';
import {
  LaneAttentionService,
  type LaneAttentionItem,
  type LaneAttentionState,
} from '../../../services/lane-attention.service';
import type { ResourceTarget } from '../../../fleet/resource-target';
import { fakeAccountMoney, unavailableAccountMoney } from '../../../testing/account-money-fixtures';
import { fakeBotPanelView, fakeCatalogBot, fakePanelAction } from '../../../testing/bot-panel-fixtures';
import { GalleryLiveStore } from '../../broker/v2-panel/gallery/lib/gallery-live-store.service';
import { BrokerV2PanelService, type AccountMoneyView } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import type {
  BotCatalogView,
  BotClearRequest,
  CohortActionResult,
  CohortLegResult,
} from '../../broker/v2-panel/lib/broker-v2-panel.types';
import { formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import { AlpacaDeskAccountDataService } from '../alpaca-desk/alpaca-desk-account-data.service';
import { AlpacaHomeComponent } from './alpaca-home.component';

const ACCOUNT_URL = `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}`;

/** The workspace's part in Home's life: it provides the one account read. */
@Component({
  selector: 'app-workspace-stub',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet],
  providers: [AlpacaDeskAccountDataService],
  template: '<router-outlet />',
})
class WorkspaceStubComponent {}

@Component({
  selector: 'app-host',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet],
  template: '<router-outlet />',
})
class HostComponent {}

@Component({ selector: 'app-bot-stub', template: '<main aria-label="Bot">Bot page</main>' })
class BotStubComponent {}

const ROUTES: Routes = [
  {
    path: 'brokers/alpaca/clerks/:clerkId',
    component: WorkspaceStubComponent,
    children: [
      {
        path: 'accounts/:accountId',
        children: [
          { path: 'bots/:sid', component: BotStubComponent },
          { path: '', component: AlpacaHomeComponent },
        ],
      },
    ],
  },
];

/** The roster as the backend groups it; the two bar bots are the money
 * fixture's own slices, joined by `strategy_instance_id`. */
function catalog(): BotCatalogView[] {
  return [
    fakeCatalogBot({
      strategy_instance_id: 'spy-ema-20260929-0931', day_pnl: 12.5,
      status_explanation: 'Running · holds 1 SPY',
    }),
    fakeCatalogBot({
      strategy_instance_id: 'spy-ema-20260925-1402', group: 'holding', running: false,
      phase: 'OFF_DUTY', desired_state: 'STOPPED', open_pnl: -2, realized_pnl_today: null,
      status_explanation: 'Stopped · still holds 1 SPY · no bot is managing it', ended_at_ms: 1_700_000_000_000,
    }),
    fakeCatalogBot({
      strategy_instance_id: 'dry-spy', group: 'dry_run', mode: 'dry_run',
      world_label: 'DRY RUN · simulated cash', simulated_cash_usd: '2000.00', day_pnl: 3,
    }),
    fakeCatalogBot({
      strategy_instance_id: 'older-bot', group: 'finished', running: false, phase: 'OFF_DUTY',
      desired_state: 'STOPPED', ended_at_ms: 1_690_000_000_000,
    }),
    fakeCatalogBot({
      strategy_instance_id: 'old-bot', group: 'finished', running: false, phase: 'OFF_DUTY',
      desired_state: 'STOPPED', ended_at_ms: 1_695_000_000_000, trade_count: 4, final_result_usd: '9.98',
    }),
  ];
}

function attentionItem(overrides: Partial<LaneAttentionItem> = {}): LaneAttentionItem {
  return {
    condition_id: 'stopped-holding:spy-ema-20260925-1402',
    reason_code: 'STOPPED_STILL_HOLDING',
    kind: 'stopped_holding',
    severity: 'warning',
    strategy_instance_id: 'spy-ema-20260925-1402',
    symbol: 'SPY',
    headline: 'spy-ema-20260925-1402 is stopped but still holds 1 SPY. No bot is managing it.',
    action: { label: 'Flatten…', destination: 'bot' },
    ...overrides,
  };
}

async function renderHome(overrides: {
  url?: string;
  attention?: LaneAttentionState;
  runBotAction?: () => Promise<{ message: string }>;
  getCatalog?: (target: ResourceTarget) => Promise<BotCatalogView[]>;
  clearBots?: (target: ResourceTarget, request: BotClearRequest) => Promise<CohortActionResult>;
  money?: AccountMoneyView;
} = {}) {
  const panel = {
    getCatalog: vi.fn(overrides.getCatalog ?? (() => Promise.resolve(catalog()))),
    getAccountMoney: vi.fn(() => Promise.resolve(overrides.money ?? fakeAccountMoney({ account_id: TEST_ACCOUNT_ID }))),
    getPanel: vi.fn(() => Promise.resolve(fakeBotPanelView({ actions: [fakePanelAction('stop')] }))),
    runBotAction: vi.fn(overrides.runBotAction ?? (() => Promise.resolve({ message: 'Stop requested for spy-ema-20260929-0931.' }))),
    clearBots: vi.fn(overrides.clearBots ?? ((_target: ResourceTarget, request: BotClearRequest) =>
      Promise.resolve(clearResult(request.strategy_instance_ids.map((sid) => clearedLeg(sid)))))),
  };
  const wall = {
    start: vi.fn(() => Promise.resolve()),
    stop: vi.fn(),
    barsBySymbol: signal(new Map()),
    markersBySid: signal(new Map()),
  };
  const directory = provideFleetDirectory();
  const view = await render(HostComponent, {
    providers: [
      { provide: directory.provide, useValue: directory.useValue },
      provideRouter(ROUTES, withComponentInputBinding(), withRouterConfig({ paramsInheritanceStrategy: 'always' })),
      { provide: MessageService, useValue: { add: vi.fn() } },
      { provide: BrokerV2PanelService, useValue: panel },
      {
        provide: BrokersService,
        useValue: {
          // Account details stay folded in these specs; a settled read keeps
          // the page stable.
          getAccount: () => Promise.reject(new Error('Alpaca is not read in this spec.')),
          getClerkStatus: () => Promise.reject(new Error('The Clerk is not read in this spec.')),
        },
      },
      {
        provide: LaneAttentionService,
        useValue: {
          stateFor: () => overrides.attention ?? { unknown: false, errorReason: null, items: [attentionItem()] },
        },
      },
    ],
    // The Wall's live feed is Home's own provider; the spec stands in for it.
    configureTestBed: (testBed) => testBed.overrideComponent(AlpacaHomeComponent, {
      set: { providers: [{ provide: GalleryLiveStore, useValue: wall }] },
    }),
  });
  const router = view.fixture.debugElement.injector.get(Router);
  await router.navigateByUrl(overrides.url ?? ACCOUNT_URL);
  await view.fixture.whenStable();
  if (overrides.getCatalog === undefined) await screen.findByText('spy-ema-20260929-0931', { selector: 'a' });
  return { view, router, panel, wall };
}

/** Two finished bots from the base roster, plus a finished Dry Run ended most recently. */
function withFinishedDryRun(): BotCatalogView[] {
  return [
    ...catalog(),
    fakeCatalogBot({
      strategy_instance_id: 'dry-old', group: 'finished', mode: 'dry_run', running: false, phase: 'OFF_DUTY',
      desired_state: 'STOPPED', ended_at_ms: 1_699_000_000_000, world_label: 'DRY RUN · simulated cash',
      final_result_usd: '-1.25', trade_count: 2,
    }),
  ];
}

function clearedLeg(sid: string, outcome: 'applied' | 'replayed' = 'applied'): CohortLegResult {
  return {
    strategy_instance_id: sid,
    outcome,
    result: {
      action_id: 'archive', outcome: 'success', receipt_id: `receipt-${sid}`, recorded_at_ms: 1_700_000_000_500,
      applied: true, revision: 4, concurrency_token: 'next', message: `${sid} is cleared.`,
    },
    error: null,
  };
}

function refusedLeg(sid: string): CohortLegResult {
  return {
    strategy_instance_id: sid,
    outcome: 'refused',
    result: null,
    error: {
      action_id: 'archive', outcome: 'conflict', receipt_id: null, recorded_at_ms: 1,
      message: 'This bot still holds shares.', why: 'Flatten it first, then clear it.',
      reason_code: 'ARCHIVE_WOULD_STRAND_CUSTODY',
    },
  };
}

/** The leg that ends a batch early: the account lost its authority, so no later leg ran. */
function authorityLostLeg(sid: string): CohortLegResult {
  return {
    strategy_instance_id: sid,
    outcome: 'failed',
    result: null,
    error: {
      action_id: 'archive', outcome: 'failure', receipt_id: null, recorded_at_ms: 1,
      message: 'This account’s execution authority can no longer be written to.',
      why: 'Restart the data plane to acquire a fresh lease and reconcile custody on boot.',
      reason_code: 'EXECUTION_LEASE_LOST',
    },
  };
}

function clearResult(legs: CohortLegResult[]): CohortActionResult {
  const count = (...kinds: string[]) => legs.filter((leg) => kinds.includes(leg.outcome)).length;
  return {
    account_id: TEST_ACCOUNT_ID, receipt_id: 'clear-key', recorded_at_ms: 1_700_000_001_000, legs,
    applied_count: count('applied'), replayed_count: count('replayed'), refused_count: count('refused'),
    failed_count: count('failed', 'unknown'),
  };
}

/** Open the Finished fold and return it. */
function finishedFold(): HTMLDetailsElement {
  const fold = screen.getByText('Finished', { selector: 'strong' }).closest('details');
  if (fold === null) throw new Error('Finished is not a fold.');
  fold.open = true;
  return fold;
}

function sids(container: HTMLElement): string[] {
  return within(container).getAllByRole('link')
    .map((link) => link.textContent?.trim() ?? '')
    .filter((text) => text.startsWith('spy-') || text.startsWith('dry-'));
}

describe('AlpacaHomeComponent', () => {
  it('draws where the money is with every slice named and its amount verbatim', async () => {
    await renderHome();

    const money = screen.getByRole('region', { name: 'Where the money is' });
    expect(within(money).getByText('$100,000.00')).toBeTruthy();
    expect(within(money).getByText('Cash plus shares at the price paid.')).toBeTruthy();
    const legend = within(money).getByRole('list', { name: 'Where the money is' });
    const entries = within(legend).getAllByRole('listitem').map((item) => item.textContent?.replace(/\s+/g, ' ').trim());
    expect(entries).toEqual([
      'spy-ema-20260929-0931 $999.99 in shares $764.71 · in entry orders $0.00 · free $235.28',
      'held by stopped bot spy-ema-20260925-1402 $670.43 released $0.00 · still claimed $0.00',
      'account charges $0.01',
      'free to deploy $98,329.57',
    ]);
    // Open P&L is the bar's note beside it, never a slice of it.
    expect(within(money).getByText('Open P&L $12.40')).toBeTruthy();
    // Deploy would admit a new bot here, so nothing refuses beside the bar.
    expect(within(money).queryByRole('note')).toBeNull();
  });

  it('states the backend’s Deploy refusal beside the bar, so free to deploy never reads as spendable', async () => {
    const refusal = 'No daily loss limit is set for this account, so new entries are refused. Set one in Settings.';
    await renderHome({ money: fakeAccountMoney({ account_id: TEST_ACCOUNT_ID, deploy_refusal: refusal }) });

    const note = within(screen.getByRole('region', { name: 'Where the money is' })).getByRole('note');
    expect(note.textContent).toContain('New bots can\'t be deployed on this money right now.');
    expect(note.textContent).toContain(refusal);
    expect(within(note).getByRole('link', { name: 'Open Settings' }).getAttribute('href')).toBe(
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/settings`,
    );
  });

  it('says why the bar cannot be drawn in the one money state’s words, never $0', async () => {
    await renderHome({
      money: { ...unavailableAccountMoney('Alpaca has not confirmed this account’s cash recently.'), account_id: TEST_ACCOUNT_ID },
    });

    const money = screen.getByRole('region', { name: 'Where the money is' });
    expect(await within(money).findByText(/Alpaca has not confirmed this account’s cash recently\./)).toBeTruthy();
    expect(within(money).queryByRole('list')).toBeNull();
    expect(money.textContent).not.toContain('$0.00');
  });

  it('never shows one account’s bots under another after a switch (review B1)', async () => {
    const { router } = await renderHome({
      getCatalog: (target) =>
        target.accountId === TEST_ACCOUNT_ID ? Promise.resolve(catalog()) : new Promise<never>(() => undefined),
    });
    await screen.findByText('spy-ema-20260929-0931', { selector: 'a' });

    // The other account's roster never answers: nothing of this one may stand
    // in for it meanwhile. (Its pending read keeps the app from settling, so
    // the spec waits on what the owner sees, not on stability.)
    void router.navigateByUrl(`/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/PA-OTHER`);

    expect(await screen.findByText("Reading this account's bots…")).toBeTruthy();
    expect(screen.queryByText('spy-ema-20260929-0931', { selector: 'a' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Stop spy-ema-20260929-0931' })).toBeNull();
    expect(screen.queryByText(/running ·/)).toBeNull();
  });

  it('never reads a failed first read as no bots (review B1)', async () => {
    await renderHome({ getCatalog: () => Promise.reject(new Error('The roster could not be projected.')) });

    expect(await screen.findByText(/The roster could not be projected\./)).toBeTruthy();
    expect(screen.getByText('This account\'s bots are not shown until they can be read.')).toBeTruthy();
    expect(screen.queryByText('No bots are running.', { exact: false })).toBeNull();
    expect(screen.queryByText(/0 running/)).toBeNull();
    expect(screen.queryByText('Finished', { selector: 'strong' })).toBeNull();
  });

  it('lists an account-level problem with its fix in Settings (review B5)', async () => {
    await renderHome({
      attention: {
        unknown: false, errorReason: null,
        items: [attentionItem({
          condition_id: 'account:alpaca_account_trading_blocked', kind: 'account',
          reason_code: 'alpaca_account_trading_blocked', severity: 'blocking', strategy_instance_id: null,
          symbol: null, headline: 'Alpaca has blocked trading on this account',
          action: { label: 'Open Settings', destination: 'settings' },
        })],
      },
    });

    const attention = screen.getByRole('list', { name: 'Needs attention' });
    expect(within(attention).getByText('Alpaca has blocked trading on this account')).toBeTruthy();
    expect(within(attention).getByRole('link', { name: 'Open Settings' }).getAttribute('href')).toBe(
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/settings`,
    );
  });

  it('never puts a Dry Run in the bar: it has its own group, in simulated cash', async () => {
    await renderHome();

    const legend = screen.getByRole('list', { name: 'Where the money is' });
    expect(legend.textContent).not.toContain('dry-spy');
    const dryRun = screen.getByRole('region', { name: 'Dry Run' });
    expect(within(dryRun).getByText('simulated cash · never uses this account\'s money')).toBeTruthy();
    expect(within(dryRun).getByText('DRY RUN · simulated cash')).toBeTruthy();
    expect(within(dryRun).getByText(/simulated starting cash \$2,000\.00/)).toBeTruthy();
    expect(within(dryRun).getByText('today, simulated')).toBeTruthy();
  });

  it('lists running bots, then stopped bots still holding, each with its own money', async () => {
    await renderHome();

    const bots = screen.getByRole('list', { name: 'Bots' });
    expect(sids(bots)).toEqual(['spy-ema-20260929-0931', 'spy-ema-20260925-1402']);
    const [running, holding] = Array.from(bots.querySelectorAll<HTMLElement>(':scope > li'));
    expect(within(running).getByText(/balance \$999\.99 · free \$235\.28/)).toBeTruthy();
    expect(within(running).getByText('+$12.50')).toBeTruthy();
    expect(within(running).getByRole('button', { name: 'Stop spy-ema-20260929-0931' })).toBeTruthy();
    expect(within(holding).getByText('Stopped · still holds 1 SPY · no bot is managing it')).toBeTruthy();
    expect(within(holding).getByText(/held \$670\.43 · released \$0\.00/)).toBeTruthy();
    expect(within(holding).getByRole('link', { name: 'Flatten spy-ema-20260925-1402…' }).getAttribute('href')).toBe(
      `${ACCOUNT_URL}/bots/spy-ema-20260925-1402`,
    );
    // A row opens the bot's own page.
    expect(within(running).getByRole('link', { name: 'spy-ema-20260929-0931' }).getAttribute('href')).toBe(
      `${ACCOUNT_URL}/bots/spy-ema-20260929-0931`,
    );
  });

  it('re-reads where the money is when it opens, so a bot deployed on another page shows its own slice at once', async () => {
    const { view, router, panel } = await renderHome();
    // The owner leaves Home; meanwhile a Deploy commits and the money moves.
    await router.navigateByUrl(`${ACCOUNT_URL}/bots/spy-ema-20260929-0931`);
    await view.fixture.whenStable();
    const deployed = 'spy-ema-20260930-1015';
    panel.getCatalog.mockResolvedValue([...catalog(), fakeCatalogBot({ strategy_instance_id: deployed })]);
    const before = fakeAccountMoney({ account_id: TEST_ACCOUNT_ID });
    panel.getAccountMoney.mockResolvedValue(fakeAccountMoney({
      account_id: TEST_ACCOUNT_ID,
      segments: [
        {
          kind: 'bot', strategy_instance_id: deployed, label: deployed, amount_usd: '1000.00', share_bps: 100,
          parts: {
            in_shares_usd: '0.00', in_shares_bps: 0, pending_usd: '0.00', pending_bps: 0,
            free_usd: '1000.00', free_bps: 10_000,
          },
          palette_index: 2,
        },
        ...(before.segments ?? []),
      ],
    }));

    await router.navigateByUrl(ACCOUNT_URL);
    await view.fixture.whenStable();

    // Its row joins the money as it is now, never a read from before its Deploy.
    const row = (await screen.findByRole('link', { name: deployed })).closest('li');
    if (row === null) throw new Error('The deployed bot has no row.');
    await vi.waitFor(() => expect(within(row).getByText(/balance \$1,000\.00 · free \$1,000\.00/)).toBeTruthy());
    expect(within(row).queryByText(/Money not shown/)).toBeNull();
  });

  it('folds Finished away, newest first, with each result as Python wrote it and Deploy again', async () => {
    await renderHome();

    const fold = screen.getByText('Finished', { selector: 'strong' }).closest('details');
    if (fold === null) throw new Error('Finished is not a fold.');
    expect(fold.open).toBe(false);
    const rows = within(fold).getAllByRole('row').slice(1);
    expect(rows.map((row) => within(row).getAllByRole('cell')[1].textContent?.trim())).toEqual(['old-bot', 'older-bot']);
    expect(within(rows[0]).getByText('$9.98')).toBeTruthy();
    expect(within(rows[0]).getByText('4')).toBeTruthy();
    // A result the fee evidence cannot vouch for is unknown, never $0.
    expect(within(rows[1]).getAllByText('unknown')).toHaveLength(2);
    expect(within(rows[0]).getByRole('link', { name: 'Deploy again from old-bot' }).getAttribute('href')).toBe(
      `${ACCOUNT_URL}/deploy?from=old-bot`,
    );
    // A cleared bot is looked up in History, filtered to the cleared ones (#2574).
    expect(within(fold).getByRole('link', { name: 'see cleared bots in History' }).getAttribute('href')).toBe(
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/history?status=cleared`,
    );
  });

  it('says what needs the owner in one line, with its severity in words and its one fix', async () => {
    await renderHome();

    const attention = screen.getByRole('list', { name: 'Needs attention' });
    const [line] = Array.from(attention.querySelectorAll<HTMLElement>(':scope > li'));
    expect(within(line).getByText('Warning')).toBeTruthy();
    expect(within(line).getByText('spy-ema-20260925-1402 is stopped but still holds 1 SPY. No bot is managing it.')).toBeTruthy();
    expect(within(line).getByRole('link', { name: 'Flatten…' }).getAttribute('href')).toBe(
      `${ACCOUNT_URL}/bots/spy-ema-20260925-1402`,
    );
  });

  it('links an out-of-sync order to Activity’s order records', async () => {
    await renderHome({
      attention: {
        unknown: false, errorReason: null,
        items: [attentionItem({
          condition_id: 'u-1', kind: 'out_of_sync', reason_code: 'UNEXPLAINED_ORDER_HOLD', severity: 'blocking',
          strategy_instance_id: null, headline: 'An order this account did not submit is unreviewed',
          action: { label: 'Open order records', destination: 'activity' },
        })],
      },
    });

    expect(screen.getByText('Blocking')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open order records' }).getAttribute('href')).toBe(`${ACCOUNT_URL}/activity`);
  });

  it('shows the Wall in exactly the List’s order, and streams candles only while it is shown', async () => {
    const { wall, router, view } = await renderHome();
    const listOrder = sids(screen.getByRole('list', { name: 'Bots' }));
    expect(wall.start).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('radio', { name: 'Wall' }));
    await view.fixture.whenStable();

    await vi.waitFor(() => expect(router.url).toBe(`${ACCOUNT_URL}?view=wall`));
    await screen.findByText('STOPPED · STILL HOLDING');
    expect(sids(screen.getByRole('list', { name: 'Bots' }))).toEqual(listOrder);
    expect(screen.getByRole('radio', { name: 'Wall' }).getAttribute('aria-checked')).toBe('true');
    expect(wall.start).toHaveBeenCalledWith('alpaca', TEST_CLERK_ID, TEST_ACCOUNT_ID, expect.anything(), expect.anything());
  });

  it('offers List and Wall as one radio group the arrow keys move, keeping the keyboard on the choice', async () => {
    const { router, view } = await renderHome();
    const group = screen.getByRole('radiogroup', { name: 'Show bots as' });
    const list = within(group).getByRole('radio', { name: 'List' });
    const wall = within(group).getByRole('radio', { name: 'Wall' });
    expect(list.getAttribute('aria-checked')).toBe('true');
    // One tab stop: the checked choice.
    expect([list.tabIndex, wall.tabIndex]).toEqual([0, -1]);

    list.focus();
    fireEvent.keyDown(list, { key: 'ArrowRight' });
    await view.fixture.whenStable();

    await vi.waitFor(() => expect(router.url).toBe(`${ACCOUNT_URL}?view=wall`));
    await vi.waitFor(() => expect(wall.getAttribute('aria-checked')).toBe('true'));
    expect(document.activeElement).toBe(wall);

    fireEvent.keyDown(wall, { key: 'ArrowLeft' });
    await vi.waitFor(() => expect(router.url).toBe(ACCOUNT_URL));
    await vi.waitFor(() => expect(document.activeElement).toBe(list));
  });

  it('keeps no arrangement of its own on the Wall: nothing drags, nothing is stored, nothing resets (D11)', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    await renderHome({ url: `${ACCOUNT_URL}?view=wall` });
    await screen.findByText('STOPPED · STILL HOLDING');

    expect(document.querySelectorAll('[draggable="true"], .cdk-drag, .cdk-drop-list')).toHaveLength(0);
    expect(screen.queryByRole('button', { name: /reset/i })).toBeNull();
    expect(setItem).not.toHaveBeenCalled();
    setItem.mockRestore();
  });

  it('names every bot’s symbol through the shared asset identity at its compact size, on the List and the Wall', async () => {
    const { view } = await renderHome();

    const rows = Array.from(view.container.querySelectorAll<HTMLElement>('app-home-bot-row'));
    expect(rows).toHaveLength(3);
    for (const row of rows) {
      expect(row.querySelector('app-asset-identity.asset-identity--xs')?.textContent).toContain('SPY');
    }

    fireEvent.click(screen.getByRole('radio', { name: 'Wall' }));
    await view.fixture.whenStable();
    await screen.findByText('STOPPED · STILL HOLDING');

    const tiles = Array.from(view.container.querySelectorAll<HTMLElement>('app-home-bot-tile'));
    expect(tiles).toHaveLength(3);
    for (const tile of tiles) {
      expect(tile.querySelector('app-asset-identity.asset-identity--xs')?.textContent).toContain('SPY');
    }
  });

  it('asks before Stop, then lands the keyboard on the outcome', async () => {
    const { panel } = await renderHome();

    fireEvent.click(screen.getByRole('button', { name: 'Stop spy-ema-20260929-0931' }));
    const cancel = await screen.findByRole('button', { name: 'Cancel' });
    await vi.waitFor(() => expect(document.activeElement).toBe(cancel));
    fireEvent.click(screen.getByRole('button', { name: 'Stop bot' }));

    const outcome = await screen.findByText('Stop requested for spy-ema-20260929-0931.');
    await vi.waitFor(() => expect(document.activeElement).toBe(outcome));
    expect(outcome.getAttribute('role')).toBe('status');
    expect(panel.runBotAction).toHaveBeenCalledWith(
      expect.objectContaining({ clerkId: TEST_CLERK_ID, accountId: TEST_ACCOUNT_ID }),
      'spy-ema-20260929-0931',
      expect.objectContaining({ action_id: 'stop' }),
    );
  });

  it('says a refused Stop in the backend’s words, and lands the keyboard there too', async () => {
    await renderHome({ runBotAction: () => Promise.reject(new Error('The lane refused the stop.')) });

    fireEvent.click(screen.getByRole('button', { name: 'Stop spy-ema-20260929-0931' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop bot' }));

    const outcome = await screen.findByText('The lane refused the stop.');
    await vi.waitFor(() => expect(document.activeElement).toBe(outcome));
    expect(outcome.getAttribute('role')).toBe('alert');
  });

  it('keeps cohort Flatten reachable from the Bots section’s menu', async () => {
    await renderHome();

    expect(screen.getByRole('button', { name: 'Flatten a group of bots…' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Archive/ })).toBeNull();
  });

  describe('clearing finished bots (owner decision 2026-09-28)', () => {
    it('clears the ticked bots behind one plain confirmation, then says what it did', async () => {
      let rows = withFinishedDryRun();
      const { panel } = await renderHome({ getCatalog: () => Promise.resolve(rows) });
      await screen.findByText('spy-ema-20260929-0931', { selector: 'a' });
      const fold = finishedFold();

      // A finished Dry Run is listed with its world, and is selectable too.
      const dryRow = within(fold).getByRole('link', { name: 'dry-old' }).closest('tr');
      if (dryRow === null) throw new Error('dry-old has no row.');
      expect(within(dryRow).getByText('DRY RUN · simulated cash')).toBeTruthy();
      fireEvent.click(within(fold).getByRole('checkbox', { name: 'Select dry-old' }));
      fireEvent.click(within(fold).getByRole('checkbox', { name: 'Select older-bot' }));
      expect(within(fold).getByRole('status').textContent).toContain('2 selected.');

      fireEvent.click(within(fold).getByRole('button', { name: 'Clear selected (2)' }));
      const dialog = await screen.findByRole('dialog');
      expect(within(dialog).getByRole('heading').textContent).toBe('Clear 2 finished bots from Home?');
      expect(dialog.textContent).toContain('Their history stays in Activity');
      expect(dialog.textContent).toContain('This can’t be undone.');
      // A plain confirmation: nothing to type.
      expect(within(dialog).queryByRole('textbox')).toBeNull();
      rows = rows.filter((bot) => !['dry-old', 'older-bot'].includes(bot.strategy_instance_id));
      fireEvent.click(within(dialog).getByRole('button', { name: 'Clear 2' }));

      const summary = await screen.findByText('Cleared 2 of 2 bots.');
      await vi.waitFor(() => expect(document.activeElement).toBe(summary.closest('p')));
      expect(summary.closest('p')?.getAttribute('role')).toBe('status');
      expect(panel.clearBots).toHaveBeenCalledTimes(1);
      const [target, request] = panel.clearBots.mock.calls[0];
      expect(request.strategy_instance_ids).toEqual(['dry-old', 'older-bot']);
      expect(target).toMatchObject({ clerkId: TEST_CLERK_ID, accountId: TEST_ACCOUNT_ID, idempotencyKey: request.idempotency_key });
      // Cleared bots leave the list on the read that follows.
      await vi.waitFor(() => expect(within(fold).queryByRole('link', { name: 'dry-old' })).toBeNull());
      expect(within(fold).queryByRole('link', { name: 'older-bot' })).toBeNull();
      expect(within(fold).getByRole('link', { name: 'old-bot' })).toBeTruthy();
    });

    it('clears every finished bot from Clear all finished', async () => {
      const { panel } = await renderHome({ getCatalog: () => Promise.resolve(withFinishedDryRun()) });
      await screen.findByText('spy-ema-20260929-0931', { selector: 'a' });
      const fold = finishedFold();

      fireEvent.click(within(fold).getByRole('button', { name: 'Clear all finished' }));
      const dialog = await screen.findByRole('dialog');
      expect(within(dialog).getByRole('heading').textContent).toBe('Clear 3 finished bots from Home?');
      fireEvent.click(within(dialog).getByRole('button', { name: 'Clear 3' }));

      await screen.findByText('Cleared 3 of 3 bots.');
      expect(panel.clearBots.mock.calls[0][1].strategy_instance_ids).toEqual(['dry-old', 'old-bot', 'older-bot']);
    });

    it('says a refused bot in the backend’s own words, and offers no pointless retry', async () => {
      await renderHome({
        clearBots: () => Promise.resolve(clearResult([clearedLeg('old-bot'), refusedLeg('older-bot')])),
      });
      const fold = finishedFold();
      fireEvent.click(within(fold).getByRole('checkbox', { name: 'Select all finished bots' }));
      fireEvent.click(within(fold).getByRole('button', { name: 'Clear selected (2)' }));
      fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Clear 2' }));

      const summary = await screen.findByText('Cleared 1 of 2 bots. 1 not cleared.');
      expect(summary.closest('p')?.getAttribute('role')).toBe('alert');
      await vi.waitFor(() => expect(document.activeElement).toBe(summary.closest('p')));
      const outcome = screen.getByRole('region', { name: 'Clear outcome' });
      const refused = within(outcome).getByText('older-bot').closest('li');
      if (refused === null) throw new Error('older-bot has no outcome line.');
      expect(within(refused).getByText('Not cleared')).toBeTruthy();
      expect(within(refused).getByText(formatReceiptLabel('ARCHIVE_WOULD_STRAND_CUSTODY'))).toBeTruthy();
      expect(within(refused).getByText('This bot still holds shares.')).toBeTruthy();
      expect(within(refused).getByText('Flatten it first, then clear it.')).toBeTruthy();
      expect(within(outcome).queryByRole('button', { name: 'Try again' })).toBeNull();
    });

    it('re-sends the same batch under the same key when the first send reached no result', async () => {
      const clearBots = vi.fn()
        .mockRejectedValueOnce(new Error('network down'))
        .mockResolvedValueOnce(clearResult([clearedLeg('old-bot', 'replayed'), clearedLeg('older-bot')]));
      await renderHome({ clearBots });
      const fold = finishedFold();
      fireEvent.click(within(fold).getByRole('checkbox', { name: 'Select all finished bots' }));
      fireEvent.click(within(fold).getByRole('button', { name: 'Clear selected (2)' }));
      fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Clear 2' }));

      const unknown = await screen.findByText(/The clear did not reach a result/);
      expect(unknown.closest('p')?.getAttribute('role')).toBe('alert');
      fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

      await screen.findByText('Cleared 2 of 2 bots.');
      expect(clearBots).toHaveBeenCalledTimes(2);
      const [firstTarget, first] = clearBots.mock.calls[0];
      const [retryTarget, retry] = clearBots.mock.calls[1];
      expect(retry).toEqual(first);
      expect(retryTarget).toEqual(firstTarget);
    });

    it('names the bots a batch ended before reaching, and re-sends the same batch for them', async () => {
      const clearBots = vi.fn()
        .mockResolvedValueOnce(clearResult([clearedLeg('dry-old'), authorityLostLeg('old-bot')]))
        .mockResolvedValueOnce(clearResult([
          clearedLeg('dry-old', 'replayed'), clearedLeg('old-bot'), clearedLeg('older-bot'),
        ]));
      await renderHome({ getCatalog: () => Promise.resolve(withFinishedDryRun()), clearBots });
      await screen.findByText('spy-ema-20260929-0931', { selector: 'a' });
      fireEvent.click(within(finishedFold()).getByRole('button', { name: 'Clear all finished' }));
      fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Clear 3' }));

      const summary = await screen.findByText('Cleared 1 of 3 bots. 2 not cleared.');
      expect(summary.closest('p')?.getAttribute('role')).toBe('alert');
      const outcome = screen.getByRole('region', { name: 'Clear outcome' });
      const failed = within(outcome).getByText('old-bot').closest('li');
      if (failed === null) throw new Error('old-bot has no outcome line.');
      expect(within(failed).getByText('Failed')).toBeTruthy();
      expect(within(failed).getByText(formatReceiptLabel('EXECUTION_LEASE_LOST'))).toBeTruthy();
      const unreached = within(outcome).getByText(/Not reached, safe to try again:/);
      expect(within(unreached).getByText('older-bot')).toBeTruthy();
      expect(within(unreached).queryByText('old-bot')).toBeNull();

      fireEvent.click(within(outcome).getByRole('button', { name: 'Try again' }));

      await screen.findByText('Cleared 3 of 3 bots.');
      expect(clearBots).toHaveBeenCalledTimes(2);
      const [firstTarget, first] = clearBots.mock.calls[0];
      const [retryTarget, retry] = clearBots.mock.calls[1];
      expect(retry).toEqual(first);
      expect(retryTarget).toEqual(firstTarget);
      expect(retry.strategy_instance_ids).toEqual(['dry-old', 'old-bot', 'older-bot']);
    });

    it('sends nothing when the confirmation is cancelled, and hands the keyboard back', async () => {
      const { panel } = await renderHome();
      const fold = finishedFold();
      fireEvent.click(within(fold).getByRole('checkbox', { name: 'Select old-bot' }));
      const opener = within(fold).getByRole('button', { name: 'Clear selected (1)' });
      opener.focus();
      fireEvent.click(opener);
      const dialog = await screen.findByRole('dialog');
      expect(within(dialog).getByRole('heading').textContent).toBe('Clear 1 finished bot from Home?');

      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

      expect(screen.queryByRole('dialog')).toBeNull();
      await vi.waitFor(() => expect(document.activeElement).toBe(opener));
      expect(panel.clearBots).not.toHaveBeenCalled();
    });

    it('has no detectable accessibility violations with bots ticked and an outcome shown', async () => {
      await renderHome({
        clearBots: () => Promise.resolve(clearResult([clearedLeg('old-bot'), refusedLeg('older-bot')])),
      });
      const fold = finishedFold();
      fireEvent.click(within(fold).getByRole('checkbox', { name: 'Select old-bot' }));
      let results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
      expect(results.violations).toEqual([]);

      fireEvent.click(within(fold).getByRole('button', { name: 'Clear all finished' }));
      fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Clear 2' }));
      await screen.findByText('Cleared 1 of 2 bots. 1 not cleared.');
      results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
      expect(results.violations).toEqual([]);
    });
  });

  it.each([
    ['the List', ACCOUNT_URL],
    ['the Wall', `${ACCOUNT_URL}?view=wall`],
  ])('has no detectable accessibility violations on %s', async (_view, url) => {
    await renderHome({ url });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations).toEqual([]);
  });
});
