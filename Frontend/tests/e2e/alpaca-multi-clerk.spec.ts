import { expect, test, type Page, type Route } from '@playwright/test';
import { DEPLOY_VIEW } from '../../src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.fixtures';
import { fakeAccountMoney } from '../../src/app/testing/account-money-fixtures';
import { fakeCatalogBot } from '../../src/app/testing/bot-panel-fixtures';

const PAPER_CLERK = 'clrk-paper-0001';
const PAPER_ACCOUNT = 'paper-account-0001';
const LIVE_CLERK = 'clrk-live-0001';
const LIVE_ACCOUNT = 'live-account-0001';
const PAPER_SCOPE = `/api/brokers/alpaca/clerks/${PAPER_CLERK}`;
const PAPER_ACCOUNT_SCOPE = `${PAPER_SCOPE}/accounts/${PAPER_ACCOUNT}`;
const PAPER_WORKSPACE = `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}`;

const directory = {
  observed_at_ms: 1_789_310_400_000,
  clerks: [
    {
      broker: 'alpaca',
      clerk_id: PAPER_CLERK,
      display_label: 'Paper lane',
      lifecycle_state: 'ready',
      volume_id: 'vol-paper',
      last_seen_at_ms: 1_789_310_400_000,
      routing_epoch: 11,
      effective_binding_generation: 4,
      capabilities: [
        'account_read',
        'bot_panel_read',
        'bot_action',
        'configuration_manage',
        'custody_read',
        'custody_command',
        'deploy',
        'gallery_read',
        'manual_orders',
      ],
      provider_summary: {
        provider_id: 'alpaca',
        adapter_version: 'alpaca-fleet.4',
        confirmed_account_id: PAPER_ACCOUNT,
        confirmed_binding_generation: 4,
        endpoint_mode: 'paper',
        authority_state: 'real_paper',
        running_count: 1,
        dry_run_count: 0,
      },
      observed_at_ms: 1_789_310_400_000,
    },
    {
      broker: 'alpaca',
      clerk_id: LIVE_CLERK,
      display_label: 'Live lane',
      lifecycle_state: 'unreachable',
      volume_id: 'vol-live',
      last_seen_at_ms: 1_789_310_390_000,
      routing_epoch: 19,
      effective_binding_generation: 8,
      capabilities: ['account_read', 'configuration_manage'],
      provider_summary: {
        provider_id: 'alpaca',
        adapter_version: 'alpaca-fleet.4',
        confirmed_account_id: LIVE_ACCOUNT,
        confirmed_binding_generation: 8,
        endpoint_mode: 'live',
        authority_state: 'real_live',
      },
      observed_at_ms: 1_789_310_400_000,
    },
  ],
};

const paperAccount = {
  broker: 'alpaca',
  account_id: PAPER_ACCOUNT,
  account_mode: 'paper',
  account_status: 'ACTIVE',
  currency: 'USD',
  cash: 10_000,
  equity: 10_000,
  buying_power: 20_000,
  portfolio_value: 10_000,
  long_market_value: 0,
  short_market_value: 0,
  trading_blocked: false,
  account_blocked: false,
  created_at_ms: null,
  observed_at_ms: 1_789_310_400_000,
};

/** The Paper account's one money read (PRD #2560 D12) as the backend authors
 * it: one running bot and one stopped bot still holding. */
const paperMoney = fakeAccountMoney({ account_id: PAPER_ACCOUNT });

/** The Paper account's bots as the backend groups them — the money read's own
 * two bot slices, joined by `strategy_instance_id`. */
const paperCatalog = [
  fakeCatalogBot({ strategy_instance_id: 'spy-ema-20260929-0931', account_id: PAPER_ACCOUNT }),
  fakeCatalogBot({
    strategy_instance_id: 'spy-ema-20260925-1402',
    account_id: PAPER_ACCOUNT,
    group: 'holding',
    running: false,
    phase: 'OFF_DUTY',
    desired_state: 'STOPPED',
    status_explanation: 'Stopped · still holds 1 SPY · no bot is managing it',
  }),
];

async function installFleetBoundary(page: Page, requests: string[]): Promise<void> {
  await page.route('**/*', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/broker-clerks') {
      await route.fulfill({ json: directory });
      return;
    }
    if (url.pathname.startsWith('/api/brokers/alpaca')) {
      requests.push(`${request.method()} ${url.pathname}${url.search}`);
      // The shell's trust-anchor poll reads one verdict per clerk lane
      // (#2161). It is deliberately excluded from every "no clerk-scoped
      // request" assertion below: it proves the shell is alive, not that a
      // surface reached into a lane.
      if (url.pathname.endsWith('/live-verdict')) {
        const paperRead = url.pathname.startsWith(`${PAPER_SCOPE}/`);
        await route.fulfill({
          json: {
            configured_mode: paperRead ? 'paper' : 'live',
            observed_account_id: paperRead ? PAPER_ACCOUNT : LIVE_ACCOUNT,
            mode_agreement: 'agreed',
            clerk_authority: 'sqlite',
            clerk_refusal_reason_code: null,
            armed_instance_count: 0,
            envelope_state: 'not_applicable',
            envelope_agreement: 'not_applicable',
            shadow_state: 'not_applicable',
            loss_hold: 'not_applicable',
            final_verdict: paperRead ? 'paper' : 'unknown',
            headline: paperRead ? `Paper account ${PAPER_ACCOUNT}` : 'Fixture verdict unread',
            detail: 'Per-clerk verdict read mocked at the fleet boundary.',
            observed_at_ms: 1_789_310_400_000,
          },
        });
        return;
      }
      if (url.pathname === `${PAPER_SCOPE}/account`) {
        await route.fulfill({ json: paperAccount });
        return;
      }
      if (url.pathname === `${PAPER_ACCOUNT_SCOPE}/money`) {
        await route.fulfill({ json: paperMoney });
        return;
      }
      if (url.pathname === `${PAPER_ACCOUNT_SCOPE}/bots/catalog`) {
        await route.fulfill({ json: paperCatalog });
        return;
      }
      if (url.pathname === `${PAPER_ACCOUNT_SCOPE}/bots/deploy`) {
        await route.fulfill({ json: { ...DEPLOY_VIEW, broker: 'alpaca', account_id: PAPER_ACCOUNT } });
        return;
      }
      if (url.pathname === `${PAPER_ACCOUNT_SCOPE}/gallery/snapshot`) {
        await route.fulfill({
          json: {
            stream_epoch: 'paper-gallery-epoch',
            surface_version: 1,
            resolution: '1m',
            bots: [],
            symbols: [],
            markers: {},
          },
        });
        return;
      }
      if (url.pathname === `${PAPER_ACCOUNT_SCOPE}/gallery/stream`) {
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: '',
        });
        return;
      }
      await route.fulfill({ status: 503, json: { detail: 'Fixture intentionally unavailable.' } });
      return;
    }
    if (url.pathname.startsWith('/api/')) {
      await route.fulfill({ status: 503, json: { detail: 'Outside this fleet fixture.' } });
      return;
    }
    await route.continue();
  });
}

/** Drop the shell's per-clerk live-verdict trust-anchor polls from a
 * request ledger: they prove the shell is alive on every route, not that a
 * surface reached into a lane. */
const withoutVerdictPolls = (requests: string[]): string[] =>
  requests.filter((entry) => !entry.includes('/live-verdict'));

/** Deploy is open and rendering its four steps (PRD #2560): What, How, Money
 * and Confirm, with the header's "Deploy a bot" marked as the current page.
 * What is drawn from this account's own Deploy read. */
async function expectDeployOpen(page: Page): Promise<void> {
  for (const step of ['What', 'How', 'Money', 'Confirm']) {
    await expect(page.getByRole('heading', { name: step, exact: true, level: 2 })).toBeVisible();
  }
  // The steps stand open side by side: What shows its strategy and symbol.
  const what = page.getByRole('region', { name: 'What', exact: true });
  await expect(what.getByRole('combobox', { name: 'Deployment strategy' })).toHaveValue('deployment_validation');
  await expect(what.getByRole('combobox', { name: 'Trading symbol' })).toContainText('SPY');
  await expect(page.getByRole('link', { name: 'Deploy a bot', exact: true }))
    .toHaveAttribute('aria-current', 'page');
}

/** The account's Home, showing that account's own bots; the tabs are Home,
 * Activity and Settings, with no separate Bots tab (PRD #2560). */
async function expectHomeRoster(page: Page): Promise<void> {
  const home = page.getByRole('main', { name: 'Home' });
  await expect(home.getByRole('heading', { name: 'Bots', exact: true })).toBeVisible();
  await expect(home).toContainText('spy-ema-20260929-0931');
  await expect(home).toContainText('spy-ema-20260925-1402');
  await expect(
    page.getByRole('navigation', { name: 'Account sections' }).getByRole('link'),
  ).toHaveText(['Home', 'Activity', 'Settings']);
}

test.describe('Alpaca multi-clerk frontend cutover', () => {
  test('keeps a failed lane visible while the healthy lane stays explicitly routable', async ({ page }) => {
    const brokerRequests: string[] = [];
    await installFleetBoundary(page, brokerRequests);

    await page.goto('/brokers/alpaca');

    await expect(page.getByRole('heading', { name: 'Alpaca' })).toBeVisible();
    const accounts = page.getByRole('list', { name: 'Alpaca accounts' });
    const paperLane = accounts.getByRole('listitem').filter({ hasText: 'Paper lane' });
    const liveLane = accounts.getByRole('listitem').filter({ hasText: 'Live lane' });
    // The healthy lane's card carries its money from its own account-money
    // read (PRD #2560 D10/D12) and its bots, worded in its mode.
    await expect(paperLane).toContainText('PAPER · practice money');
    await expect(paperLane).toContainText('Account money $100,000.00');
    await expect(paperLane).toContainText('Free to deploy $98,329.57');
    await expect(paperLane.getByRole('list', { name: 'Where Paper lane’s money is' }))
      .toContainText('free to deploy $98,329.57');
    await expect(paperLane).toContainText('1 running');
    await expect(paperLane).toContainText('1 stopped, still holding');
    // The failed lane keeps its card, states the lifecycle the directory
    // already carries rather than a read its clerk cannot answer, and keeps
    // its own way in — its own account's workspace, never another lane's URL.
    await expect(liveLane).toContainText('This lane is Unreachable.');
    await expect(liveLane).not.toContainText('Account money');
    await expect(liveLane.getByRole('link')).toHaveAttribute(
      'href',
      `/brokers/alpaca/clerks/${LIVE_CLERK}/accounts/${LIVE_ACCOUNT}`,
    );

    // The whole card is the way into the account (#2187).
    const deskLink = paperLane.getByRole('link');
    await expect(deskLink).toHaveAttribute('href', PAPER_WORKSPACE);
    await deskLink.click();

    await expect(page).toHaveURL(new RegExp(`${PAPER_WORKSPACE}$`));
    await expect(page.getByRole('main', { name: 'Home' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Deploy a bot', exact: true })).toHaveAttribute(
      'href',
      `${PAPER_WORKSPACE}/deploy`,
    );
    await expect.poll(() => brokerRequests.some((entry) => entry === `GET ${PAPER_ACCOUNT_SCOPE}/money`))
      .toBe(true);

    expect(withoutVerdictPolls(brokerRequests).filter(
      (entry) => !entry.includes(`/clerks/${PAPER_CLERK}/`),
    )).toEqual([]);
  });

  test('turns a global Deploy intent into an explicit account choice', async ({ page }) => {
    const brokerRequests: string[] = [];
    await installFleetBoundary(page, brokerRequests);

    await page.goto('/brokers/alpaca?deploy=');
    await expect(
      page.getByText(/choose a ready Paper or Live account below to deploy a strategy/i),
    ).toBeVisible();
    // No lane is picked for the owner (FR-096): the intent waits on the list,
    // and no account's Deploy read has been made.
    await expect(page).toHaveURL(/\/brokers\/alpaca\?deploy=$/);
    expect(brokerRequests.filter((entry) => entry.includes('/bots/deploy'))).toEqual([]);

    // The list has no Deploy link of its own (#2187): choosing the account is
    // the step the intent was waiting for, and it travels with that choice,
    // landing on the account's own Deploy — the page its header's "Deploy a
    // bot" opens (PRD #2560).
    await page.getByRole('list', { name: 'Alpaca accounts' })
      .getByRole('listitem').filter({ hasText: 'Paper lane' }).getByRole('link').click();

    await expect(page).toHaveURL(new RegExp(`${PAPER_WORKSPACE}/deploy$`));
    await expectDeployOpen(page);
  });

  test('turns a global Bots intent into an explicit account choice', async ({ page }) => {
    const brokerRequests: string[] = [];
    await installFleetBoundary(page, brokerRequests);
    // The retired `?surface=bots` bookmark folds onto `/brokers/alpaca/bots`,
    // which is a redirect to the account list now (#2187) — a roster belongs
    // to an account, so reaching one starts by choosing the account.
    await page.goto('/brokers/alpaca?surface=bots');
    await expect(page).toHaveURL('/brokers/alpaca');
    await expect(page.getByRole('heading', { name: 'Alpaca' })).toBeVisible();

    await page.getByRole('list', { name: 'Alpaca accounts' })
      .getByRole('listitem').filter({ hasText: 'Paper lane' }).getByRole('link').click();
    await expect(page).toHaveURL(PAPER_WORKSPACE);
    // Bots merged into Home (PRD #2560): the chosen account's own roster is
    // on its Home, and the workspace offers no separate Bots tab.
    await expectHomeRoster(page);

    // The account's retired Bots bookmark lands on that same Home.
    await page.goto(`${PAPER_WORKSPACE}/bots`);
    await expect(page).toHaveURL(PAPER_WORKSPACE);
    await expectHomeRoster(page);
    expect(withoutVerdictPolls(brokerRequests).filter(
      (entry) => !entry.includes(`/clerks/${PAPER_CLERK}/`),
    )).toEqual([]);
  });

  test('keeps Deploy on its own routed URL, surviving a reload', async ({ page }) => {
    const brokerRequests: string[] = [];
    await installFleetBoundary(page, brokerRequests);
    await page.goto(PAPER_WORKSPACE);

    await page.getByRole('link', { name: 'Deploy a bot', exact: true }).click();

    await expect(page).toHaveURL(new RegExp(`${PAPER_WORKSPACE}/deploy$`));
    await expectDeployOpen(page);
    await page.reload();
    await expect(page).toHaveURL(new RegExp(`${PAPER_WORKSPACE}/deploy$`));
    await expectDeployOpen(page);
    expect(withoutVerdictPolls(brokerRequests).filter(
      (entry) => !entry.includes(`/clerks/${PAPER_CLERK}/`),
    )).toEqual([]);

    // Leaving Deploy is switching tabs, like any other — not closing an
    // overlay (ADR 0064 Decision 1 extended).
    await page.getByRole('navigation', { name: 'Account sections' })
      .getByRole('link', { name: 'Home', exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${PAPER_WORKSPACE}$`));
    await expect(page.getByRole('main', { name: 'Home' })).toBeVisible();
  });

  test('opens the live gallery stream with the exact clerk and account identity', async ({ page }) => {
    const brokerRequests: string[] = [];
    const allRequests: string[] = [];
    const pageErrors: string[] = [];
    page.on('request', (request) => allRequests.push(`${request.method()} ${new URL(request.url()).pathname}`));
    page.on('pageerror', (error) => pageErrors.push(error.message));
    await installFleetBoundary(page, brokerRequests);

    // The Gallery is Home's Wall view now (PRD #2560); its old bookmark
    // opens that view on the same account.
    await page.goto(`${PAPER_WORKSPACE}/gallery`);

    await expect(page).toHaveURL(`${PAPER_WORKSPACE}?view=wall`);
    await expect(page.getByRole('main', { name: 'Home' })).toBeVisible();
    await expect(page.getByRole('radio', { name: 'Wall' })).toBeChecked();
    await expect.poll(() => ({
      snapshot: allRequests.some(
        (entry) => entry.startsWith(`GET ${PAPER_ACCOUNT_SCOPE}/gallery/snapshot`),
      ),
      stream: allRequests.some(
        (entry) => entry.startsWith(`GET ${PAPER_ACCOUNT_SCOPE}/gallery/stream`),
      ),
    })).toEqual({ snapshot: true, stream: true });
    expect(pageErrors).toEqual([]);
    expect(withoutVerdictPolls(brokerRequests).filter(
      (entry) => entry.includes(`/clerks/${LIVE_CLERK}/`),
    )).toEqual([]);
  });

  test('fails unscoped operational links in place instead of choosing a lane', async ({ page }) => {
    const brokerRequests: string[] = [];
    await installFleetBoundary(page, brokerRequests);

    // `/brokers/alpaca/bots` redirects to the account list now, so it stays
    // out of this failure loop; these are the compatibility URLs that still
    // have no lane to resolve to.
    for (const path of [
      '/brokers/alpaca/deploy',
      '/brokers/alpaca/accounts/unresolved/deploy',
    ]) {
      await page.goto(path);
      await expect(page.getByRole('heading', { name: 'Broker lane unavailable' })).toBeVisible();
      await expect(page).toHaveURL(new RegExp(`${path}$`));
    }
    expect(
      withoutVerdictPolls(brokerRequests).filter((entry) => entry.includes('/clerks/')),
    ).toEqual([]);
  });

  test('fails a wrong-provider canonical link in place', async ({ page }) => {
    const brokerRequests: string[] = [];
    await installFleetBoundary(page, brokerRequests);
    const path = `/brokers/webull/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}/gallery`;

    await page.goto(path);

    await expect(page.getByRole('heading', { name: 'Broker lane unavailable' })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${path}$`));
    expect(
      withoutVerdictPolls(brokerRequests).filter((entry) => entry.includes('/clerks/')),
    ).toEqual([]);
  });
});
