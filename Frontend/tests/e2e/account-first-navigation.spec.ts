import { expect, test, type Page, type Route } from '@playwright/test';

import type { BrokerAccountSnapshot } from '../../src/app/api/alpaca.types';
import type { GalleryLiveSnapshot } from '../../src/app/components/broker/v2-panel/gallery/lib/gallery.types';
import type { AccountMoneyView } from '../../src/app/components/broker/v2-panel/lib/broker-v2-panel.service';
import type {
  BotPanelLiveSnapshot,
  ChartLiveResponse,
  PanelProfile,
} from '../../src/app/components/broker/v2-panel/lib/broker-v2-panel.types';
import type {
  FleetDirectoryResponse,
  LaneDescriptor,
  LaneProviderSummary,
} from '../../src/app/fleet/fleet-directory.types';
import type { FleetCapability } from '../../src/app/fleet/resource-target';
import type { AggregateAttentionResponse } from '../../src/app/services/lane-attention.service';
import { fakeAccountMoney } from '../../src/app/testing/account-money-fixtures';
import { fakeAlpacaLiveVerdict } from '../../src/app/testing/alpaca-live-verdict-fixtures';
import {
  fakeBotPanelView,
  fakeCatalogBot,
  fakeChartFeed,
} from '../../src/app/testing/bot-panel-fixtures';

/**
 * The account-first walk (ADR 0064, #2187; PRD #2560): one way into Alpaca,
 * and one account under the operator's feet the whole way through it.
 *
 * The list names accounts, not lanes; choosing one opens its Home, and every
 * move after that — a tab, Home's Wall, a bot's page, Back, a top-bar account
 * pill — either stays on that account or changes it because the operator said
 * so. Nothing substitutes another account (FR-096), and each account's reads
 * are its own (FR-093).
 *
 * The fleet boundary is mocked at the network edge exactly as
 * `alpaca-multi-clerk.spec.ts` does: no coordinator, no clerk container, no
 * broker. Two ready lanes, Paper and Live, each with a confirmed account.
 * Every payload is typed against the contract or built by the unit suite's
 * canonical fixture, so a contract change breaks this walk at compile time
 * instead of leaving it to drift.
 */

const PAPER_CLERK = 'clrk-paper-0001';
const PAPER_ACCOUNT = 'paper-account-0001';
const LIVE_CLERK = 'clrk-live-0001';
const LIVE_ACCOUNT = 'live-account-0001';
const NOW_MS = 1_789_310_400_000;

/** Paper's money read is the canonical fixture verbatim — one running bot and
 * one stopped bot still holding shares — so its roster names the same two. */
const PAPER_MONEY: AccountMoneyView = fakeAccountMoney({ account_id: PAPER_ACCOUNT });
const PAPER_BOT = 'spy-ema-20260929-0931';
const PAPER_HOLDING_BOT = 'spy-ema-20260925-1402';

/** Live runs no bots: all of its money is free, and its figures differ from
 * Paper's so a card showing the other account's read cannot pass. */
const LIVE_MONEY: AccountMoneyView = fakeAccountMoney({
  account_id: LIVE_ACCOUNT,
  world: 'real_live',
  total_usd: '250000.00',
  cash_usd: '250000.00',
  free_to_deploy_usd: '250000.00',
  in_bots_usd: '0.00',
  held_by_stopped_usd: '0.00',
  account_charges_usd: '0.00',
  stopped_holding_count: 0,
  open_pnl_usd: '0.00',
  equity_usd: '250000.00',
  today_pnl_usd: '0.00',
  segments: [{ kind: 'free', label: 'free to deploy', amount_usd: '250000.00', share_bps: 10_000 }],
});

const PAPER_LANE = `/brokers/alpaca/clerks/${PAPER_CLERK}`;
const LIVE_LANE = `/brokers/alpaca/clerks/${LIVE_CLERK}`;
const PAPER_WORKSPACE = `${PAPER_LANE}/accounts/${PAPER_ACCOUNT}`;
const LIVE_WORKSPACE = `${LIVE_LANE}/accounts/${LIVE_ACCOUNT}`;

const CAPABILITIES: FleetCapability[] = [
  'account_read',
  'positions_read',
  'orders_read',
  'bot_panel_read',
  'bot_action',
  'configuration_manage',
  'custody_read',
  'deploy',
  'gallery_read',
];

function lane(
  clerkId: string,
  displayLabel: string,
  summary: LaneProviderSummary,
  overrides: Partial<LaneDescriptor> = {},
): LaneDescriptor {
  return {
    broker: 'alpaca',
    clerk_id: clerkId,
    display_label: displayLabel,
    lifecycle_state: 'ready',
    volume_id: 'vol-x',
    last_seen_at_ms: NOW_MS,
    routing_epoch: 4,
    effective_binding_generation: 3,
    capabilities: [...CAPABILITIES],
    // The card's counts are the lane's own heartbeat summary; nothing needs
    // the owner on either account.
    provider_summary: { dry_run_count: 0, attention_count: 0, ...summary },
    observed_at_ms: NOW_MS,
    ...overrides,
  };
}

const directory: FleetDirectoryResponse = {
  observed_at_ms: NOW_MS,
  clerks: [
    lane(PAPER_CLERK, 'Paper', {
      provider_id: 'alpaca',
      adapter_version: 'alpaca-fleet.4',
      confirmed_account_id: PAPER_ACCOUNT,
      confirmed_binding_generation: 3,
      endpoint_mode: 'paper',
      authority_state: 'real_paper',
      running_count: 1,
    }),
    lane(
      LIVE_CLERK,
      'Live',
      {
        provider_id: 'alpaca',
        adapter_version: 'alpaca-fleet.4',
        confirmed_account_id: LIVE_ACCOUNT,
        confirmed_binding_generation: 8,
        endpoint_mode: 'live',
        authority_state: 'real_live',
        running_count: 0,
      },
      { routing_epoch: 19, effective_binding_generation: 8 },
    ),
  ],
};

/** Both lanes answered the aggregate attention poll, and neither has
 * anything needing the owner. */
const attention: AggregateAttentionResponse = {
  observed_at_ms: NOW_MS,
  lanes: [
    { broker: 'alpaca', clerk_id: PAPER_CLERK, ok: true, value: { account_id: PAPER_ACCOUNT, items: [] } },
    { broker: 'alpaca', clerk_id: LIVE_CLERK, ok: true, value: { account_id: LIVE_ACCOUNT, items: [] } },
  ],
};

function account(accountId: string, equity: number): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: accountId,
    account_mode: accountId === PAPER_ACCOUNT ? 'paper' : 'live',
    account_status: 'ACTIVE',
    account_blocked: false,
    trading_blocked: false,
    pattern_day_trader: false,
    currency: 'USD',
    cash: equity,
    equity,
    buying_power: equity,
    portfolio_value: equity,
    long_market_value: 0,
    short_market_value: 0,
    created_at_ms: null,
    observed_at_ms: NOW_MS,
  };
}

/** Paper's roster, matching its money read: the running bot and the stopped
 * bot still holding the shares the read prices. */
const paperCatalog = [
  fakeCatalogBot({ strategy_instance_id: PAPER_BOT, account_id: PAPER_ACCOUNT, last_activity_at_ms: NOW_MS }),
  fakeCatalogBot({
    strategy_instance_id: PAPER_HOLDING_BOT,
    account_id: PAPER_ACCOUNT,
    phase: 'OFF_DUTY',
    desired_state: 'STOPPED',
    running: false,
    status_label: 'Stopped',
    status_explanation: 'Stopped · still holding 1 SPY',
    exposure: { SPY: 1 },
    fills_today: 0,
    realized_pnl_today: 0,
    group: 'holding',
    last_activity_at_ms: NOW_MS,
  }),
];

/** One tile's worth of Wall: the running bot and the bars its chart draws. */
function gallerySnapshot(epoch: string): GalleryLiveSnapshot {
  return {
    stream_epoch: epoch,
    surface_version: 1,
    as_of_ms: NOW_MS,
    resolution: '1m',
    bots: [
      {
        sid: PAPER_BOT,
        symbol: 'SPY',
        label: 'Deployment Validation',
        phase: 'ON_DUTY',
        desired_state: 'RUNNING',
        running: true,
        needs_attention: false,
        fills_today: 0,
        realized_pnl_today: 0,
        open_pnl: 0,
        day_pnl: 0,
        session_change_pct: 0.4,
        last_bar_at_ms: NOW_MS,
        feed: {
          state: 'LIVE',
          headline: 'Chart feed live',
          detail: 'fixture feed',
          attention_required: false,
          last_error: null,
        },
        primary_action: { action_id: 'stop', label: 'Stop', enabled: true, disabled_reason: null },
      },
    ],
    symbols: [
      {
        symbol: 'SPY',
        bars: [
          {
            start_ms: NOW_MS - 120_000,
            end_ms: NOW_MS - 60_000,
            open: '500.00',
            high: '502.00',
            low: '499.00',
            close: '501.00',
            volume: 1_000,
            source: 'ibkr',
          },
          {
            start_ms: NOW_MS - 60_000,
            end_ms: NOW_MS,
            open: '501.00',
            high: '503.00',
            low: '500.00',
            close: '502.50',
            volume: 1_200,
            source: 'ibkr',
          },
        ],
      },
    ],
    markers: {},
  };
}

/** The broker's panel profile a bot's page reads before it renders. */
const PANEL_PROFILE: PanelProfile = {
  broker: 'alpaca',
  fee_fidelity: 'none',
  live_bars_supported: false,
  stations: [],
  supported_action_ids: ['deploy', 'archive'],
};

/** The running bot's own page: its panel and an empty live chart. A bot's
 * panel is a large contract shape, and the unit suite already owns a
 * canonical one; copying it here would give this walk its own drifting second
 * copy of a contract it only needs in order to reach the bot's page. */
function botLiveSnapshot(resolution: ChartLiveResponse['resolution']): BotPanelLiveSnapshot {
  const base = fakeBotPanelView();
  return {
    stream_epoch: 'paper-bot-epoch',
    surface_version: 1,
    panel: fakeBotPanelView({
      strategy_instance_id: PAPER_BOT,
      account_id: PAPER_ACCOUNT,
      health: { ...base.health, strategy_instance_id: PAPER_BOT },
      clerk: { ...base.clerk, account_id: PAPER_ACCOUNT },
    }),
    live_chart: {
      strategy_instance_id: PAPER_BOT,
      symbol: 'SPY',
      trading_date_open_ms: NOW_MS - 3_600_000,
      trading_date_close_ms: NOW_MS + 3_600_000,
      resolution,
      bars: [],
      fill_markers: [],
      overlay_notices: [],
      feed: fakeChartFeed(),
      as_of_ms: NOW_MS,
    },
  };
}

const SCOPE =(clerk: string) => `/api/brokers/alpaca/clerks/${clerk}`;
const ACCOUNT_SCOPE = (clerk: string, accountId: string) =>
  `${SCOPE(clerk)}/accounts/${accountId}`;

/**
 * Every read either lane's surfaces make, answered from fixtures. Anything
 * else under `/api/` fails loudly with a 503 rather than reaching a real
 * service, so a surface that quietly grew a new dependency shows up as a
 * failed read rather than as a silent pass.
 */
async function installFleetBoundary(page: Page): Promise<string[]> {
  const requests: string[] = [];
  await page.route('**/*', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === '/api/broker-clerks') {
      await route.fulfill({ json: directory });
      return;
    }
    if (!path.startsWith('/api/')) {
      await route.continue();
      return;
    }
    requests.push(`${request.method()} ${path}`);

    if (path === '/api/broker-clerks/aggregate/attention') {
      await route.fulfill({ json: attention });
      return;
    }
    if (path === `${SCOPE(PAPER_CLERK)}/live-verdict`) {
      await route.fulfill({ json: fakeAlpacaLiveVerdict('paper', { observed_account_id: PAPER_ACCOUNT }) });
      return;
    }
    if (path === `${SCOPE(LIVE_CLERK)}/live-verdict`) {
      await route.fulfill({ json: fakeAlpacaLiveVerdict('live', { observed_account_id: LIVE_ACCOUNT }) });
      return;
    }
    if (path === `${SCOPE(PAPER_CLERK)}/account`) {
      await route.fulfill({ json: account(PAPER_ACCOUNT, 100_000) });
      return;
    }
    if (path === `${SCOPE(LIVE_CLERK)}/account`) {
      await route.fulfill({ json: account(LIVE_ACCOUNT, 250_000) });
      return;
    }
    if (path === `${ACCOUNT_SCOPE(PAPER_CLERK, PAPER_ACCOUNT)}/money`) {
      await route.fulfill({ json: PAPER_MONEY });
      return;
    }
    if (path === `${ACCOUNT_SCOPE(LIVE_CLERK, LIVE_ACCOUNT)}/money`) {
      await route.fulfill({ json: LIVE_MONEY });
      return;
    }
    if (path === `${ACCOUNT_SCOPE(PAPER_CLERK, PAPER_ACCOUNT)}/bots/catalog`) {
      await route.fulfill({ json: paperCatalog });
      return;
    }
    if (path === `${ACCOUNT_SCOPE(LIVE_CLERK, LIVE_ACCOUNT)}/bots/catalog`) {
      await route.fulfill({ json: [] });
      return;
    }
    if (path === `${ACCOUNT_SCOPE(PAPER_CLERK, PAPER_ACCOUNT)}/gallery/snapshot`) {
      await route.fulfill({ json: gallerySnapshot('paper-epoch') });
      return;
    }
    if (path === `${ACCOUNT_SCOPE(LIVE_CLERK, LIVE_ACCOUNT)}/gallery/snapshot`) {
      await route.fulfill({ json: { ...gallerySnapshot('live-epoch'), bots: [], symbols: [] } });
      return;
    }
    if (path === '/api/brokers/alpaca/panel-profile') {
      await route.fulfill({ json: PANEL_PROFILE });
      return;
    }
    if (path === `${ACCOUNT_SCOPE(PAPER_CLERK, PAPER_ACCOUNT)}/bots/${PAPER_BOT}/live-snapshot`) {
      const resolution = url.searchParams.get('resolution') === '1m' ? '1m' : '5s';
      await route.fulfill({ json: botLiveSnapshot(resolution) });
      return;
    }
    if (path.endsWith('/gallery/stream') || path.endsWith('/live-stream')) {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: '',
      });
      return;
    }
    await route.fulfill({ status: 503, json: { detail: 'Outside this fleet fixture.' } });
  });
  return requests;
}

/** The shell's per-lane trust-anchor poll fires on every route; it proves the
 * shell is alive, not that a surface reached into a lane. */
const withoutVerdictPolls = (requests: string[]): string[] =>
  requests.filter((entry) => !entry.includes('/live-verdict'));

/** The top-bar account pills (PRD #2560 D4), each a link named by its
 * account and the server's verdict for it. */
const accountPill = (page: Page, account: 'Paper' | 'Live') =>
  page
    .getByRole('navigation', { name: 'Accounts', exact: true })
    .getByRole('link', { name: new RegExp(`^${account}:`) });

/** One tab of the open account's tab strip. */
const workspaceTab = (page: Page, tab: 'Home' | 'Activity' | 'Settings') =>
  page
    .getByRole('navigation', { name: 'Account sections' })
    .getByRole('link', { name: tab, exact: true });

test.describe('Account-first Alpaca navigation', () => {
  test('walks the account list into one account and stays on it', async ({ page }) => {
    const requests = await installFleetBoundary(page);

    // The account list: one heading, one card per account, no chooser.
    await page.goto('/brokers/alpaca');
    await expect(page.getByRole('heading', { name: 'Alpaca' })).toBeVisible();
    await expect(page.getByRole('list', { name: 'Alpaca accounts' })).toBeVisible();
    await expect(page.getByRole('heading')).toHaveCount(1);

    const accounts = page.getByRole('list', { name: 'Alpaca accounts' }).getByRole('link');
    await expect(accounts).toHaveCount(2);
    // Each card reads its own account's money (D10/D12, FR-093): Paper's
    // figures on Paper's card, Live's on Live's.
    const paperCard = accounts.filter({ hasText: 'Paper' });
    await expect(paperCard).toContainText('Account money $100,000.00');
    await expect(paperCard).toContainText('Free to deploy $98,329.57');
    await expect(paperCard).toContainText('1 running');
    await expect(paperCard).toContainText('1 stopped, still holding');
    await expect(paperCard).toContainText('All clear');
    const liveCard = accounts.filter({ hasText: 'Live' });
    await expect(liveCard).toContainText('Account money $250,000.00');
    await expect(liveCard).toContainText('0 running');
    // No lane mechanics: the card names an account, not a wiring diagram.
    await expect(page.getByText('Binding generation')).toHaveCount(0);
    await expect(page.getByText('Real Paper')).toHaveCount(0);

    // The whole card opens that account's Home.
    await paperCard.click();
    await expect(page).toHaveURL(PAPER_WORKSPACE);
    await expect(workspaceTab(page, 'Home')).toHaveAttribute('aria-current', 'page');
    // From here on, every read belongs to the account the operator chose. The
    // list's own reads before this point are each card reading its *own* lane
    // — the per-account independence FR-093 asks for, not a lane reaching
    // across — so the ledger is measured from the moment the choice was made.
    const sinceTheChoice = requests.length;

    const home = page.getByRole('main', { name: 'Home' });
    const listView = page.getByRole('radio', { name: 'List' });
    const wallView = page.getByRole('radio', { name: 'Wall' });
    // The bot's own page names the bot it is for.
    const botPage = page.getByRole('heading', { name: PAPER_BOT, exact: true });

    // Home's List, then one bot's own page — which belongs to Home, so Home
    // stays the current tab while it is open.
    await expect(listView).toHaveAttribute('aria-checked', 'true');
    await home.getByRole('link', { name: PAPER_BOT, exact: true }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/bots/${PAPER_BOT}`);
    await expect(botPage).toBeVisible();
    await expect(workspaceTab(page, 'Home')).toHaveAttribute('aria-current', 'page');

    // Back to the List it was opened from.
    await page.goBack();
    await expect(page).toHaveURL(PAPER_WORKSPACE);
    await expect(listView).toHaveAttribute('aria-checked', 'true');

    // Home's Wall, then one tile — Back returns to the Wall rather than to
    // the List.
    await wallView.click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}?view=wall`);
    await expect(wallView).toHaveAttribute('aria-checked', 'true');
    await home.getByRole('link', { name: PAPER_BOT, exact: true }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/bots/${PAPER_BOT}`);
    await expect(botPage).toBeVisible();
    await expect(workspaceTab(page, 'Home')).toHaveAttribute('aria-current', 'page');
    await page.goBack();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}?view=wall`);
    await expect(wallView).toHaveAttribute('aria-checked', 'true');

    // Nothing the workspace did reached into the other account (FR-096).
    expect(
      withoutVerdictPolls(requests.slice(sinceTheChoice)).filter((entry) =>
        entry.includes(`/clerks/${LIVE_CLERK}/`),
      ),
    ).toEqual([]);
  });

  test('switches accounts in place from the top-bar pills, keeping the tab', async ({ page }) => {
    await installFleetBoundary(page);

    // Each pill is the same move from anywhere in a workspace: the chosen
    // account, on the tab the operator is standing on (ADR 0064 Decision 4;
    // PRD #2560 D4 — the pills are the only way between accounts). Activity
    // is account-scoped; Settings is lane-scoped (FR-092).
    const standingOn = [
      { tab: 'Activity', paperTab: `${PAPER_WORKSPACE}/activity`, liveTab: `${LIVE_WORKSPACE}/activity` },
      { tab: 'Settings', paperTab: `${PAPER_LANE}/settings`, liveTab: `${LIVE_LANE}/settings` },
    ] as const;
    for (const { tab, paperTab, liveTab } of standingOn) {
      await page.goto(paperTab);
      await expect(workspaceTab(page, tab)).toHaveAttribute('aria-current', 'page');
      await expect(accountPill(page, 'Paper')).toHaveAttribute('aria-current', 'true');

      await accountPill(page, 'Live').click();
      await expect(page).toHaveURL(liveTab);
      await expect(workspaceTab(page, tab)).toHaveAttribute('aria-current', 'page');
      await expect(accountPill(page, 'Live')).toHaveAttribute('aria-current', 'true');
      await expect(accountPill(page, 'Paper')).not.toHaveAttribute('aria-current', 'true');

      await accountPill(page, 'Paper').click();
      await expect(page).toHaveURL(paperTab);
      await expect(workspaceTab(page, tab)).toHaveAttribute('aria-current', 'page');
    }

    // Home's Wall travels with Home: it is how the tab is being looked at.
    await page.goto(`${PAPER_WORKSPACE}?view=wall`);
    await expect(page.getByRole('radio', { name: 'Wall' })).toHaveAttribute('aria-checked', 'true');

    await accountPill(page, 'Live').click();
    await expect(page).toHaveURL(`${LIVE_WORKSPACE}?view=wall`);
    await expect(workspaceTab(page, 'Home')).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('radio', { name: 'Wall' })).toHaveAttribute('aria-checked', 'true');

    await accountPill(page, 'Paper').click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}?view=wall`);
    await expect(page.getByRole('radio', { name: 'Wall' })).toHaveAttribute('aria-checked', 'true');
  });

  test('opens an account from outside any workspace on its Home', async ({ page }) => {
    await installFleetBoundary(page);

    // Standing nowhere near an account: a pill has no tab to keep, so it
    // opens the account's own Home.
    await page.goto('/data-lab');
    await accountPill(page, 'Live').click();

    await expect(page).toHaveURL(LIVE_WORKSPACE);
    await expect(workspaceTab(page, 'Home')).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('main', { name: 'Home' })).toBeVisible();
  });

  test('offers Alpaca as Accounts alone, and retires the broker-wide surfaces', async ({ page }) => {
    await installFleetBoundary(page);

    await page.goto('/data-lab');
    await page.getByRole('menuitem', { name: 'Alpaca' }).click();
    const alpaca = page.getByRole('menuitem', { name: 'Alpaca' });
    await expect(alpaca.getByRole('menuitem')).toHaveCount(1);
    await expect(alpaca.getByRole('menuitem', { name: 'Accounts' })).toBeVisible();

    // The chooser bookmarks — and the retired `?surface=` hints that fold
    // onto them — land on the account list rather than 404-ing.
    for (const path of [
      '/brokers/alpaca/bots',
      '/brokers/alpaca/gallery',
      '/brokers/alpaca?surface=bots',
      '/brokers/alpaca?surface=gallery',
    ]) {
      await page.goto(path);
      await expect(page).toHaveURL('/brokers/alpaca');
      await expect(page.getByRole('heading', { name: 'Alpaca' })).toBeVisible();
    }
  });
});
