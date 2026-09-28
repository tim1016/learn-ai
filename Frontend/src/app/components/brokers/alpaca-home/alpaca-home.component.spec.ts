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
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import { fakeBotPanelView, fakeCatalogBot, fakePanelAction } from '../../../testing/bot-panel-fixtures';
import { GalleryLiveStore } from '../../broker/v2-panel/gallery/lib/gallery-live-store.service';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import type { BotCatalogView } from '../../broker/v2-panel/lib/broker-v2-panel.types';
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
} = {}) {
  const panel = {
    getCatalog: vi.fn(() => Promise.resolve(catalog())),
    getAccountMoney: vi.fn(() => Promise.resolve(fakeAccountMoney({ account_id: TEST_ACCOUNT_ID }))),
    getPanel: vi.fn(() => Promise.resolve(fakeBotPanelView({ actions: [fakePanelAction('stop')] }))),
    runBotAction: vi.fn(overrides.runBotAction ?? (() => Promise.resolve({ message: 'Stop requested for spy-ema-20260929-0931.' }))),
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
  await screen.findByText('spy-ema-20260929-0931', { selector: 'a' });
  return { view, router, panel, wall };
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
      'held by stopped bot spy-ema-20260925-1402 $670.43',
      'account charges $0.01',
      'free to deploy $98,329.57',
    ]);
    // Open P&L is a note beside the bar, never a slice of it.
    expect(within(money).getByText(/Open gain or loss on shares: \$12\.40/)).toBeTruthy();
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

  it('folds Finished away, newest first, with each result as Python wrote it and Deploy again', async () => {
    await renderHome();

    const fold = screen.getByText('Finished', { selector: 'strong' }).closest('details');
    if (fold === null) throw new Error('Finished is not a fold.');
    expect(fold.open).toBe(false);
    const rows = within(fold).getAllByRole('row').slice(1);
    expect(rows.map((row) => within(row).getAllByRole('cell')[0].textContent?.trim())).toEqual(['old-bot', 'older-bot']);
    expect(within(rows[0]).getByText('$9.98')).toBeTruthy();
    expect(within(rows[0]).getByText('4')).toBeTruthy();
    // A result the fee evidence cannot vouch for is unknown, never $0.
    expect(within(rows[1]).getAllByText('unknown')).toHaveLength(2);
    expect(within(rows[0]).getByRole('link', { name: 'Deploy again from old-bot' }).getAttribute('href')).toBe(
      `${ACCOUNT_URL}/deploy?from=old-bot`,
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

    fireEvent.click(screen.getByRole('button', { name: 'Wall' }));
    await view.fixture.whenStable();

    await vi.waitFor(() => expect(router.url).toBe(`${ACCOUNT_URL}?view=wall`));
    await screen.findByText('STOPPED · STILL HOLDING');
    expect(sids(screen.getByRole('list', { name: 'Bots' }))).toEqual(listOrder);
    expect(screen.getByRole('button', { name: 'Wall' }).getAttribute('aria-pressed')).toBe('true');
    expect(wall.start).toHaveBeenCalledWith('alpaca', TEST_CLERK_ID, TEST_ACCOUNT_ID, expect.anything(), expect.anything());
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

  it('has no detectable accessibility violations', async () => {
    await renderHome();

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations).toEqual([]);
  });
});
