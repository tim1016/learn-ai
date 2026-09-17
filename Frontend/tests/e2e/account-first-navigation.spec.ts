import { expect, test, type Page, type Route } from '@playwright/test';

import { fakeBotPanelView } from '../../src/app/testing/bot-panel-fixtures';

/**
 * The account-first walk (ADR 0064, #2187): one way into Alpaca, and one
 * account under the operator's feet the whole way through it.
 *
 * The list names accounts, not lanes; choosing one opens its workspace and
 * every move after that — a tab, a bot's page, Back, the account switcher,
 * an account badge in the top bar — either stays on that account or changes
 * it because the operator said so. Nothing substitutes another account
 * (FR-096), and each account's reads are its own (FR-093).
 *
 * The fleet boundary is mocked at the network edge exactly as
 * `alpaca-multi-clerk.spec.ts` does: no coordinator, no clerk container, no
 * broker. Two ready lanes, Paper and Live, each with a confirmed account.
 */

const PAPER_CLERK = 'clrk-paper-0001';
const PAPER_ACCOUNT = 'paper-account-0001';
const LIVE_CLERK = 'clrk-live-0001';
const LIVE_ACCOUNT = 'live-account-0001';
const PAPER_BOT = 'paper-spy-01';
const NOW_MS = 1_789_310_400_000;

const PAPER_WORKSPACE = `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}`;
const LIVE_WORKSPACE = `/brokers/alpaca/clerks/${LIVE_CLERK}/accounts/${LIVE_ACCOUNT}`;

const CAPABILITIES = [
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

function lane(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    broker: 'alpaca',
    lifecycle_state: 'ready',
    volume_id: 'vol-x',
    last_seen_at_ms: NOW_MS,
    routing_epoch: 4,
    effective_binding_generation: 3,
    capabilities: [...CAPABILITIES],
    observed_at_ms: NOW_MS,
    ...overrides,
  };
}

const directory = {
  observed_at_ms: NOW_MS,
  clerks: [
    lane({
      clerk_id: PAPER_CLERK,
      display_label: 'Paper',
      provider_summary: {
        provider_id: 'alpaca',
        adapter_version: 'alpaca-fleet.4',
        confirmed_account_id: PAPER_ACCOUNT,
        confirmed_binding_generation: 3,
        endpoint_mode: 'paper',
        authority_state: 'real_paper',
      },
    }),
    lane({
      clerk_id: LIVE_CLERK,
      display_label: 'Live',
      routing_epoch: 19,
      effective_binding_generation: 8,
      provider_summary: {
        provider_id: 'alpaca',
        adapter_version: 'alpaca-fleet.4',
        confirmed_account_id: LIVE_ACCOUNT,
        confirmed_binding_generation: 8,
        endpoint_mode: 'live',
        authority_state: 'shadow',
      },
    }),
  ],
};

function account(accountId: string, equity: number): Record<string, unknown> {
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

function verdict(finalVerdict: string, accountId: string | null): Record<string, unknown> {
  return {
    configured_mode: finalVerdict === 'paper' ? 'paper' : 'live',
    observed_account_id: accountId,
    mode_agreement: 'agreed',
    clerk_authority: finalVerdict === 'paper' ? 'sqlite' : 'shadow',
    clerk_refusal_reason_code: null,
    armed_instance_count: 0,
    envelope_state: 'not_applicable',
    envelope_agreement: 'not_applicable',
    shadow_state: 'not_applicable',
    loss_hold: 'not_applicable',
    final_verdict: finalVerdict,
    headline: `fixture verdict ${finalVerdict}`,
    detail: 'fixture detail',
    observed_at_ms: NOW_MS,
  };
}

function catalogBot(sid: string, accountId: string): Record<string, unknown> {
  return {
    strategy_instance_id: sid,
    strategy_key: 'deployment_validation',
    strategy_label: 'Deployment Validation',
    broker: 'alpaca',
    account_id: accountId,
    symbol: 'SPY',
    mode: 'trade',
    phase: 'ON_DUTY',
    desired_state: 'RUNNING',
    running: true,
    status_label: 'Working',
    status_explanation: 'fixture bot',
    exposure: {},
    fills_today: 0,
    realized_pnl_today: 0,
    open_pnl: 0,
    day_pnl: 0,
    last_activity_at_ms: NOW_MS,
    needs_attention: false,
    row_action: null,
  };
}

/** One tile's worth of gallery: a bot and the bars its chart draws. */
function gallerySnapshot(sid: string, epoch: string): Record<string, unknown> {
  return {
    stream_epoch: epoch,
    surface_version: 1,
    as_of_ms: NOW_MS,
    resolution: '1m',
    bots: [
      {
        sid,
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
        primary_action: { action_id: 'stop', label: 'Stop', enabled: true, disabled_reason: null },
      },
    ],
    symbols: [
      {
        symbol: 'SPY',
        bars: [
          { t: NOW_MS - 120_000, o: 500, h: 502, l: 499, c: 501, v: 1_000 },
          { t: NOW_MS - 60_000, o: 501, h: 503, l: 500, c: 502.5, v: 1_200 },
        ],
      },
    ],
    markers: {},
  };
}

const SCOPE = (clerk: string) => `/api/brokers/alpaca/clerks/${clerk}`;
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

    if (path === `${SCOPE(PAPER_CLERK)}/live-verdict`) {
      await route.fulfill({ json: verdict('paper', PAPER_ACCOUNT) });
      return;
    }
    if (path === `${SCOPE(LIVE_CLERK)}/live-verdict`) {
      await route.fulfill({ json: verdict('live-unarmed', LIVE_ACCOUNT) });
      return;
    }
    if (path === `${SCOPE(PAPER_CLERK)}/account`) {
      await route.fulfill({ json: account(PAPER_ACCOUNT, 10_000) });
      return;
    }
    if (path === `${SCOPE(LIVE_CLERK)}/account`) {
      await route.fulfill({ json: account(LIVE_ACCOUNT, 250_000) });
      return;
    }
    if (path === `${ACCOUNT_SCOPE(PAPER_CLERK, PAPER_ACCOUNT)}/bots/catalog`) {
      await route.fulfill({ json: [catalogBot(PAPER_BOT, PAPER_ACCOUNT)] });
      return;
    }
    if (path === `${ACCOUNT_SCOPE(LIVE_CLERK, LIVE_ACCOUNT)}/bots/catalog`) {
      await route.fulfill({ json: [] });
      return;
    }
    if (path.endsWith('/gallery/snapshot')) {
      const live = path.startsWith(SCOPE(LIVE_CLERK));
      await route.fulfill({
        json: live
          ? { ...gallerySnapshot(PAPER_BOT, 'live-epoch'), bots: [], symbols: [] }
          : gallerySnapshot(PAPER_BOT, 'paper-epoch'),
      });
      return;
    }
    if (path === `${ACCOUNT_SCOPE(PAPER_CLERK, PAPER_ACCOUNT)}/bots/${PAPER_BOT}/panel`) {
      // The one fixture in this file that is not inline: a bot's panel is a
      // large contract shape, and the unit suite already owns a canonical
      // one. Copying it here would give this walk its own drifting second
      // copy of a contract it only needs in order to reach the bot's page.
      await route.fulfill({
        json: fakeBotPanelView({
          strategy_instance_id: PAPER_BOT,
          account_id: PAPER_ACCOUNT,
        }),
      });
      return;
    }
    if (path.endsWith('/gallery/stream')) {
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
    const paperCard = accounts.filter({ hasText: 'Paper' });
    await expect(paperCard).toContainText('$10,000.00');
    await expect(paperCard).toContainText('1 bot running');
    // No lane mechanics: the card names an account, not a wiring diagram.
    await expect(page.getByText('Binding generation')).toHaveCount(0);
    await expect(page.getByText('Real Paper')).toHaveCount(0);

    // The whole card opens that account's workspace.
    await paperCard.click();
    await expect(page).toHaveURL(PAPER_WORKSPACE);
    // From here on, every read belongs to the account the operator chose. The
    // list's own reads before this point are each card reading its *own* lane
    // — the per-account independence FR-093 asks for, not a lane reaching
    // across — so the ledger is measured from the moment the choice was made.
    const sinceTheChoice = requests.length;

    // Bots, then one bot's own page — which belongs to the tab it was opened
    // from, so Bots stays current while it is open.
    await page.getByRole('link', { name: 'Bots', exact: true }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/bots`);
    await page.getByRole('link', { name: 'Open full page' }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/bots/${PAPER_BOT}?from=bots`);
    await expect(page.getByRole('link', { name: 'Bots', exact: true })).toHaveAttribute(
      'aria-current',
      'page',
    );

    // Back to the roster it was opened from.
    await page.goBack();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/bots`);

    // Gallery, then one tile — which stamps the tab it was opened from, so
    // Back returns to the Gallery rather than to the roster.
    await page.getByRole('link', { name: 'Gallery', exact: true }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/gallery`);
    await page.getByRole('button', { name: `Open SPY · ${PAPER_BOT} detail` }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/bots/${PAPER_BOT}?from=gallery`);
    await expect(page.getByRole('link', { name: 'Gallery', exact: true })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await page.goBack();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/gallery`);

    // Nothing the workspace did reached into the other account (FR-096).
    expect(
      withoutVerdictPolls(requests.slice(sinceTheChoice)).filter((entry) =>
        entry.includes(`/clerks/${LIVE_CLERK}/`),
      ),
    ).toEqual([]);
  });

  test('switches accounts in place, keeping the tab, and comes back by badge', async ({ page }) => {
    await installFleetBoundary(page);

    await page.goto(`${PAPER_WORKSPACE}/gallery`);
    await expect(page.getByRole('main', { name: 'Bot gallery' })).toBeVisible();

    // The account switcher in the workspace header: choosing Live lands on
    // Live's Gallery, not on its Overview — the operator was looking at a
    // Gallery and still is (ADR 0064 Decision 4).
    await page.getByRole('button', { name: /Paper/ }).first().click();
    await page.getByRole('link', { name: /Live/ }).first().click();
    await expect(page).toHaveURL(`${LIVE_WORKSPACE}/gallery`);

    // The Paper badge in the top bar is the same move made from the shell:
    // it returns to Paper's Gallery, the tab still under the operator's feet.
    await page
      .locator('app-alpaca-live-banner')
      .filter({ hasText: 'Paper' })
      .getByRole('link')
      .click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/gallery`);
  });

  test('opens an account from outside any workspace on its Overview', async ({ page }) => {
    await installFleetBoundary(page);

    // Standing nowhere near an account: a badge has no tab to keep, so it
    // opens the account's own page.
    await page.goto('/data-lab');
    await page
      .locator('app-alpaca-live-banner')
      .filter({ hasText: 'Live' })
      .getByRole('link')
      .click();

    await expect(page).toHaveURL(LIVE_WORKSPACE);
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
