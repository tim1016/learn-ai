import { expect, test, type Page, type Route } from '@playwright/test';

const PAPER_CLERK = 'clrk-paper-0001';
const PAPER_ACCOUNT = 'paper-account-0001';
const LIVE_CLERK = 'clrk-live-0001';
const LIVE_ACCOUNT = 'live-account-0001';
const PAPER_SCOPE = `/api/brokers/alpaca/clerks/${PAPER_CLERK}`;
const PAPER_ACCOUNT_SCOPE = `${PAPER_SCOPE}/accounts/${PAPER_ACCOUNT}`;

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
  pattern_day_trader: false,
  trading_blocked: false,
  account_blocked: false,
  created_at_ms: null,
  observed_at_ms: 1_789_310_400_000,
};

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

/** Drop the readiness read a not-ready account's card makes on the account
 * list. Each card reads the lane it renders, which is exactly the per-lane
 * independence FR-093 asks for; what the assertions below look for is a
 * *workspace* touching an account other than the one it is open on. */
const withoutReadinessReads = (requests: string[]): string[] =>
  requests.filter((entry) => !entry.endsWith('/configuration/desk-state'));

test.describe('Alpaca multi-clerk frontend cutover', () => {
  test('keeps a failed lane visible while the healthy lane stays explicitly routable', async ({ page }) => {
    const brokerRequests: string[] = [];
    await installFleetBoundary(page, brokerRequests);

    await page.goto('/brokers/alpaca');

    await expect(page.getByRole('heading', { name: 'Alpaca' })).toBeVisible();
    const paperLane = page.locator('li').filter({ hasText: 'Paper lane' });
    const liveLane = page.locator('li').filter({ hasText: 'Live lane' });
    await expect(paperLane).toContainText('$10,000.00');
    // The failed lane keeps its card and its own way in — the lane-scoped
    // Configuration its workspace can still serve, never another lane's URL.
    await expect(liveLane).toContainText('Readiness unavailable');
    await expect(liveLane.getByRole('link')).toHaveAttribute(
      'href',
      `/brokers/alpaca/clerks/${LIVE_CLERK}/configuration`,
    );

    // The whole card is the way into the account (#2187).
    const deskLink = paperLane.getByRole('link');
    await expect(deskLink).toHaveAttribute(
      'href',
      `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}`,
    );
    await deskLink.click();

    await expect(page).toHaveURL(
      new RegExp(`/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}$`),
    );
    await expect(page.getByRole('button', { name: 'Deploy strategy' })).toBeVisible();
    await expect.poll(() => brokerRequests.some((entry) => entry === `GET ${PAPER_SCOPE}/account`))
      .toBe(true);

    expect(withoutReadinessReads(withoutVerdictPolls(brokerRequests)).filter(
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
    // The list has no Deploy link of its own (#2187): choosing the account is
    // the step the intent was waiting for, and it travels with that choice.
    await page.locator('li').filter({ hasText: 'Paper lane' }).getByRole('link').click();

    await expect(page).toHaveURL(new RegExp(
      `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}\\?deploy=`,
    ));
    await expect(page.getByRole('heading', { name: 'Deploy a bot' })).toBeVisible();
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

    await page.locator('li').filter({ hasText: 'Paper lane' }).getByRole('link').click();
    await expect(page).toHaveURL(
      `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}`,
    );
    await page.getByRole('link', { name: 'Bots', exact: true }).click();

    await expect(page).toHaveURL(new RegExp(
      `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}/bots$`,
    ));
  });

  test('keeps an open Deploy workflow on the canonical clerk/account URL', async ({ page }) => {
    const brokerRequests: string[] = [];
    await installFleetBoundary(page, brokerRequests);
    await page.goto(
      `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}`,
    );

    await page.getByRole('button', { name: 'Deploy strategy' }).click();

    await expect(page.getByRole('heading', { name: 'Deploy a bot' })).toBeVisible();
    await expect(page).toHaveURL(
      new RegExp(
        `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}\\?deploy=`,
      ),
    );
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Deploy a bot' })).toBeVisible();
    expect(withoutVerdictPolls(brokerRequests).filter(
      (entry) => !entry.includes(`/clerks/${PAPER_CLERK}/`),
    )).toEqual([]);

    await page.getByRole('button', { name: 'Close deploy a bot' }).click();
    await expect(page).toHaveURL(
      new RegExp(`/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}$`),
    );
  });

  test('opens the live gallery stream with the exact clerk and account identity', async ({ page }) => {
    const brokerRequests: string[] = [];
    const allRequests: string[] = [];
    const pageErrors: string[] = [];
    page.on('request', (request) => allRequests.push(`${request.method()} ${new URL(request.url()).pathname}`));
    page.on('pageerror', (error) => pageErrors.push(error.message));
    await installFleetBoundary(page, brokerRequests);

    await page.goto(
      `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}/gallery`,
    );

    await expect(page.getByRole('main', { name: 'Bot gallery' })).toBeVisible();
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
