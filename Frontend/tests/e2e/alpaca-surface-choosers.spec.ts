import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * E2E for the read-only Alpaca surface choosers (`/brokers/alpaca/bots` and
 * `/gallery`) and the clerk-only unavailable routes behind them. The fleet
 * boundary is fully mocked at the network edge, mirroring
 * `alpaca-multi-clerk.spec.ts`'s pattern; no clerk container is reached.
 *
 * Fixture posture (four lanes, one directory):
 * - `clrk-paper`  — ready, confirmed account, every surface capability.
 * - `clrk-live`   — ready, confirmed live account, `shadow` authority; the
 *                   mutable `state` object flips it to `real_live` +
 *                   `live-armed` for the state-change test (shadow → real
 *                   live is a state change on this same lane, never a third
 *                   lane).
 * - `clrk-starting` — `starting` lifecycle with a confirmed account: the
 *                   lifecycle refusal.
 * - `clrk-unbound`  — ready and capable but no confirmed account: the
 *                   unbound refusal.
 */

const PAPER_CLERK = 'clrk-paper-0001';
const PAPER_ACCOUNT = 'paper-account-0001';
const PAPER_SCOPE = `/api/brokers/alpaca/clerks/${PAPER_CLERK}`;
const PAPER_ACCOUNT_SCOPE = `${PAPER_SCOPE}/accounts/${PAPER_ACCOUNT}`;
const LIVE_CLERK = 'clrk-live-0001';
const LIVE_ACCOUNT = 'live-account-0001';
const STARTING_CLERK = 'clrk-starting-0001';
const UNBOUND_CLERK = 'clrk-unbound-0001';
const NOW_MS = 1_789_310_400_000;

const CAPABLE_OF_SURFACES = [
  'account_read',
  'bot_panel_read',
  'bot_action',
  'configuration_manage',
  'deploy',
  'gallery_read',
];

interface FixtureState {
  liveAuthority: 'shadow' | 'real_live';
  liveVerdict: 'live-unarmed' | 'live-armed';
}

function lane(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    broker: 'alpaca',
    lifecycle_state: 'ready',
    volume_id: 'vol-x',
    last_seen_at_ms: NOW_MS,
    routing_epoch: 4,
    effective_binding_generation: 3,
    capabilities: [...CAPABLE_OF_SURFACES],
    provider_summary: {
      provider_id: 'alpaca',
      adapter_version: 'alpaca-fleet.4',
      confirmed_binding_generation: 3,
      endpoint_mode: 'paper',
      authority_state: 'real_paper',
    },
    observed_at_ms: NOW_MS,
    ...overrides,
  };
}

function verdict(
  finalVerdict: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    configured_mode: finalVerdict === 'paper' ? 'paper' : 'live',
    observed_account_id: null,
    mode_agreement: 'agreed',
    clerk_authority: 'sqlite',
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
    ...overrides,
  };
}

function directoryFor(state: FixtureState): Record<string, unknown> {
  return {
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
          authority_state: state.liveAuthority,
        },
      }),
      lane({
        clerk_id: STARTING_CLERK,
        display_label: 'Live Starting',
        lifecycle_state: 'starting',
        provider_summary: {
          provider_id: 'alpaca',
          adapter_version: 'alpaca-fleet.4',
          confirmed_account_id: 'starting-account-0001',
          confirmed_binding_generation: 2,
          endpoint_mode: 'live',
          authority_state: 'shadow',
        },
      }),
      lane({
        clerk_id: UNBOUND_CLERK,
        display_label: 'Unbound',
        provider_summary: {
          provider_id: 'alpaca',
          adapter_version: 'alpaca-fleet.4',
          confirmed_account_id: null,
          confirmed_binding_generation: null,
          endpoint_mode: 'live',
          authority_state: 'shadow',
        },
      }),
    ],
  };
}

const CATALOG_BOT = {
  strategy_instance_id: 'paper-bot-1',
  strategy_key: 'paper-strategy',
  strategy_label: 'Paper Strategy',
  broker: 'alpaca',
  account_id: PAPER_ACCOUNT,
  symbol: 'SPY',
  mode: 'trade',
  phase: 'ON_DUTY',
  desired_state: 'RUNNING',
  running: true,
  status_label: 'Running',
  status_explanation: 'fixture bot',
  exposure: {},
  fills_today: 0,
  realized_pnl_today: 0,
  open_pnl: 0,
  day_pnl: 0,
  last_activity_at_ms: null,
  needs_attention: false,
  row_action: null,
};

async function installFleetBoundary(page: Page, state: FixtureState): Promise<void> {
  await page.route('**/*', async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/broker-clerks') {
      await route.fulfill({ json: directoryFor(state) });
      return;
    }
    // The shell's trust-anchor poll reads one verdict per clerk lane; one
    // handler answers every lane from the fixture's mutable state.
    if (url.pathname.endsWith('/live-verdict')) {
      const clerkVerdicts: Record<string, Record<string, unknown>> = {
        [PAPER_CLERK]: verdict('paper'),
        [LIVE_CLERK]: verdict(state.liveVerdict, {
          observed_account_id: LIVE_ACCOUNT,
          armed_instance_count: state.liveVerdict === 'live-armed' ? 2 : 0,
        }),
        [STARTING_CLERK]: verdict('unknown'),
        [UNBOUND_CLERK]: verdict('unknown'),
      };
      const clerk = url.pathname.split('/').at(-2) ?? '';
      await route.fulfill({ json: clerkVerdicts[clerk] ?? verdict('unknown') });
      return;
    }
    if (url.pathname === `${PAPER_ACCOUNT_SCOPE}/bots/catalog`) {
      await route.fulfill({ json: [CATALOG_BOT] });
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
    if (url.pathname.startsWith('/api/brokers/alpaca')) {
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

test.describe('Alpaca surface choosers', () => {
  test('lists every lane with no automatic selection and routes a serving lane canonically', async ({ page }) => {
    await installFleetBoundary(page, { liveAuthority: 'shadow', liveVerdict: 'live-unarmed' });

    await page.goto('/brokers/alpaca/bots');

    await expect(page.getByRole('heading', { name: 'Alpaca bots' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Clerk lanes — Bots roster' })).toBeVisible();
    await expect(page.getByText('4 registered')).toBeVisible();
    await expect(page.getByText(/no lane is selected automatically/i)).toBeVisible();

    // Serving lanes link to their canonical operational URL. Each card is
    // scoped through its surface nav's unique accessible name.
    const laneCard = (label: string) =>
      page.locator('li').filter({
        has: page.getByRole('navigation', { name: `${label} surfaces` }),
      });
    const paperCard = laneCard('Paper');
    await expect(paperCard.getByRole('link', { name: 'Bots' })).toHaveAttribute(
      'href',
      `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}/bots`,
    );
    await expect(paperCard.locator('.lane-mode-chip')).toHaveText('Paper money');
  });

  test('keeps an unavailable lane selectable through its clerk-only route', async ({ page }) => {
    await installFleetBoundary(page, { liveAuthority: 'shadow', liveVerdict: 'live-unarmed' });

    await page.goto('/brokers/alpaca/bots');

    const startingCard = page.locator('li').filter({
      has: page.getByRole('navigation', { name: 'Live Starting surfaces' }),
    });
    await expect(startingCard.getByRole('link', { name: 'Bots' })).toHaveAttribute(
      'href',
      `/brokers/alpaca/clerks/${STARTING_CLERK}/bots`,
    );
    await startingCard.getByRole('link', { name: 'Bots' }).click();

    await expect(page).toHaveURL(`/brokers/alpaca/clerks/${STARTING_CLERK}/bots`);
    await expect(
      page.getByRole('heading', { name: /Bots roster unavailable — Live Starting/ }),
    ).toBeVisible();
    await expect(page.getByText(/This lane is Starting,/i)).toBeVisible();
    await expect(page.getByText(/no other lane is substituted/i)).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open lane configuration' })).toHaveAttribute(
      'href',
      `/brokers/alpaca/clerks/${STARTING_CLERK}/configuration`,
    );
    await expect(page.getByRole('link', { name: 'Back to the Bots lane chooser' })).toHaveAttribute(
      'href',
      '/brokers/alpaca/bots',
    );
  });

  test('explains an unbound lane at its clerk-only route', async ({ page }) => {
    await installFleetBoundary(page, { liveAuthority: 'shadow', liveVerdict: 'live-unarmed' });

    await page.goto(`/brokers/alpaca/clerks/${UNBOUND_CLERK}/gallery`);

    await expect(
      page.getByRole('heading', { name: /Gallery unavailable — Unbound/ }),
    ).toBeVisible();
    await expect(page.getByText(/no confirmed account binding yet/i)).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open lane configuration' })).toBeVisible();
    await expect(
      page.getByRole('link', { name: 'Back to the Gallery lane chooser' }),
    ).toHaveAttribute('href', '/brokers/alpaca/gallery');
  });

  test('chooses nothing on its own — history walks chooser and canonical URLs honestly', async ({ page }) => {
    await installFleetBoundary(page, { liveAuthority: 'shadow', liveVerdict: 'live-unarmed' });

    await page.goto('/brokers/alpaca/bots');
    await page
      .locator('li')
      .filter({ has: page.getByRole('navigation', { name: 'Paper surfaces' }) })
      .getByRole('link', { name: 'Bots' })
      .click();

    const canonicalBots = `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}/bots`;
    await expect(page).toHaveURL(canonicalBots);
    await expect(page.getByRole('heading', { name: 'Alpaca bots' })).toBeVisible();

    // The Bots ↔ Gallery cross-links keep the exact clerk/account scope.
    // (Scoped to the page main: the shell header also carries a Gallery
    // quick-link to the chooser.)
    await page.getByRole('main').getByRole('link', { name: 'Gallery' }).click();
    await expect(page).toHaveURL(
      `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}/gallery`,
    );
    await expect(
      page.getByRole('link', { name: 'Bots roster' }).first(),
    ).toHaveAttribute('href', canonicalBots);

    // Back lands on the immediately previous URL — never an auto-selection.
    await page.goBack();
    await expect(page).toHaveURL(canonicalBots);
    await page.goBack();
    await expect(page).toHaveURL('/brokers/alpaca/bots');
    await page.goForward();
    await expect(page).toHaveURL(canonicalBots);
  });

  test('retires the old surface-hint bookmarks onto the real chooser routes', async ({ page }) => {
    await installFleetBoundary(page, { liveAuthority: 'shadow', liveVerdict: 'live-unarmed' });

    await page.goto('/brokers/alpaca?surface=bots');
    await expect(page).toHaveURL('/brokers/alpaca/bots');
    await expect(page.getByRole('heading', { name: 'Clerk lanes — Bots roster' })).toBeVisible();

    await page.goto('/brokers/alpaca?surface=gallery');
    await expect(page).toHaveURL('/brokers/alpaca/gallery');
    await expect(page.getByRole('heading', { name: 'Clerk lanes — Gallery' })).toBeVisible();
  });

  test('treats shadow → real-live as a state change on the same lane', async ({ page }) => {
    const state: FixtureState = { liveAuthority: 'shadow', liveVerdict: 'live-unarmed' };
    await installFleetBoundary(page, state);

    await page.goto('/brokers/alpaca/bots');
    const liveCard = page.locator('li').filter({
      has: page.getByRole('navigation', { name: 'Live surfaces' }),
    });
    await expect(liveCard.getByText('Shadow')).toBeVisible();
    await expect(liveCard.locator('.lane-mode-chip')).toHaveText('Live');

    // The lane arms: same clerk, same card — authority and verdict flip in
    // place, and no third lane appears.
    state.liveAuthority = 'real_live';
    state.liveVerdict = 'live-armed';
    await page.reload();

    const armedCard = page.locator('li').filter({
      has: page.getByRole('navigation', { name: 'Live surfaces' }),
    });
    await expect(armedCard.getByText('Real Live')).toBeVisible();
    await expect(armedCard.locator('.lane-mode-chip')).toHaveText('Live');
    await expect(armedCard.getByRole('link', { name: 'Bots' })).toHaveAttribute(
      'href',
      `/brokers/alpaca/clerks/${LIVE_CLERK}/accounts/${LIVE_ACCOUNT}/bots`,
    );
    await expect(page.getByText('4 registered')).toBeVisible();
  });

  test('keeps every lane pill visible on a narrow screen', async ({ page }) => {
    await installFleetBoundary(page, { liveAuthority: 'shadow', liveVerdict: 'live-unarmed' });
    await page.setViewportSize({ width: 390, height: 720 });

    await page.goto('/brokers/alpaca/bots');

    // The pills are the account-mode trust anchor (ADR 0059 D8): wrapping is
    // allowed, clipping is not. Each lane stays visible, named, and fully
    // inside the header's box — `toBeVisible()` alone would not catch an
    // overflow clip.
    const pills = page.locator('app-alpaca-live-banner [role="status"]');
    await expect(pills).toHaveCount(4);
    for (const label of ['Paper', 'Live', 'Live Starting', 'Unbound']) {
      await expect(pills.filter({ hasText: label }).first()).toBeVisible();
    }
    const headerBox = await page
      .getByRole('banner', { name: 'Botasur application' })
      .boundingBox();
    expect(headerBox).not.toBeNull();
    for (const label of ['Paper', 'Live', 'Live Starting', 'Unbound']) {
      const box = await pills.filter({ hasText: label }).first().boundingBox();
      expect(box, `pill ${label} has a box`).not.toBeNull();
      expect(
        box &&
        headerBox &&
        box.x >= headerBox.x &&
        box.y >= headerBox.y &&
        box.x + box.width <= headerBox.x + headerBox.width + 1 &&
        box.y + box.height <= headerBox.y + headerBox.height + 1,
        `pill ${label} is fully inside the header`,
      ).toBe(true);
    }
  });
});
