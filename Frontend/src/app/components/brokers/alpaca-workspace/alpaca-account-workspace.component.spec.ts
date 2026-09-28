import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import {
  Router,
  RouterOutlet,
  provideRouter,
  withComponentInputBinding,
  withRouterConfig,
  type Routes,
} from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { MessageService } from 'primeng/api';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../api/alpaca.types';
import {
  provideFleetDirectory,
  testLane,
  TEST_ACCOUNT_ID,
  TEST_CLERK_ID,
  type FleetDirectoryDouble,
} from '../../../fleet/fleet-directory-testing';
import { BrokersService } from '../../../services/brokers.service';
import {
  AlpacaLiveVerdictService,
  UNPOLLED_LANE_STATE,
  type LaneVerdictState,
} from '../../../services/alpaca-live-verdict.service';
import { formatTimestampDisplay } from '../../../shared/timestamp/timestamp-display';
import { fakeAccountMoney, unavailableAccountMoney } from '../../../testing/account-money-fixtures';
import { fakeVerdictState } from '../../../testing/alpaca-live-verdict-fixtures';
import { healthyAccountOperatorPostureFixture } from '../../../testing/operator-blocker-fixtures';
import { BrokerV2PanelService, type AccountMoneyView } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { AlpacaAccountListPageComponent } from '../alpaca-desk/alpaca-account-list-page.component';
import { BrokerConfigurationService } from '../alpaca-desk/configuration/broker-configuration.service';
import { AlpacaAccountWorkspaceComponent } from './alpaca-account-workspace.component';
import { AlpacaSurfaceNotReadyTabComponent } from './alpaca-surface-not-ready-tab.component';
import { BotsPageActionsBridgeService } from './bots-page-actions-bridge.service';

const LANE_URL = `/brokers/alpaca/clerks/${TEST_CLERK_ID}`;
const WORKSPACE_URL = `${LANE_URL}/accounts/${TEST_ACCOUNT_ID}`;
/** The account list — the only page that links into a workspace's Deploy. */
const ACCOUNT_LIST_URL = '/brokers/alpaca';

/** The shell under test is the workspace, so each tab is a stub: the real
 * tabs bring their own polling, stores and fixtures, and none of that is what
 * this spec is asking about. */
@Component({
  selector: 'app-workspace-host',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet],
  template: '<router-outlet />',
})
class WorkspaceHostComponent {}

// Each real tab roots itself in a labelled `<main>`; the stubs do the same so
// the accessibility assertion below grades the workspace's own chrome against
// the landmark structure the tabs actually bring.
@Component({ selector: 'app-overview-stub', template: '<main aria-label="Overview">Overview tab</main>' })
class OverviewStubComponent {}

@Component({ selector: 'app-bots-stub', template: '<main aria-label="Bots">Bots tab</main>' })
class BotsStubComponent {}

@Component({ selector: 'app-gallery-stub', template: '<main aria-label="Gallery">Gallery tab</main>' })
class GalleryStubComponent {}

@Component({
  selector: 'app-configuration-stub',
  template: '<main aria-label="Configuration">Configuration tab</main>',
})
class ConfigurationStubComponent {}

@Component({ selector: 'app-bot-stub', template: '<main aria-label="Bot">Bot page</main>' })
class BotStubComponent {}

@Component({
  selector: 'app-deploy-stub',
  template: '<main aria-label="Deploy strategy">Deploy strategy tab</main>',
})
class DeployStubComponent {}

// The same shape as `app.routes.ts`: one shell at the clerk level, the
// lane-scoped tabs directly under it, and the account-scoped tabs under a
// componentless `accounts/:accountId`. The not-ready tab is the REAL
// component — a stub could not show that a lane-scoped URL explains itself in
// place rather than redirecting (FR-096).
const WORKSPACE_ROUTES: Routes = [
  {
    path: 'brokers/alpaca/clerks/:clerkId',
    component: AlpacaAccountWorkspaceComponent,
    children: [
      { path: 'configuration', component: ConfigurationStubComponent },
      { path: 'bots', data: { surface: 'bots' }, component: AlpacaSurfaceNotReadyTabComponent },
      {
        path: 'gallery',
        data: { surface: 'gallery' },
        component: AlpacaSurfaceNotReadyTabComponent,
      },
      {
        path: 'accounts/:accountId',
        children: [
          { path: 'bots/:sid', component: BotStubComponent },
          { path: 'bots', component: BotsStubComponent },
          { path: 'gallery', component: GalleryStubComponent },
          { path: 'deploy', component: DeployStubComponent },
          { path: '', component: OverviewStubComponent },
        ],
      },
      { path: '', redirectTo: 'configuration', pathMatch: 'full' },
    ],
  },
  // The real account list, so a spec can follow one of its account cards into
  // the workspace instead of only reading the card's `href`.
  { path: 'brokers/alpaca', component: AlpacaAccountListPageComponent },
];

/** A ready lane that Alpaca has not confirmed an account for: it keeps its
 * workspace, and only Configuration can open (ADR 0064, FR-096). */
function unboundDirectory() {
  return provideFleetDirectory({
    observed_at_ms: 1,
    clerks: [
      testLane({
        display_label: 'Unbound',
        provider_summary: { ...testLane().provider_summary, confirmed_account_id: null },
      }),
    ],
  });
}

function fakeAccount(overrides: Partial<BrokerAccountSnapshot> = {}): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: TEST_ACCOUNT_ID,
    account_mode: 'paper',
    account_status: 'ACTIVE',
    currency: 'USD',
    cash: 10_000,
    equity: 15_000,
    buying_power: 30_000,
    portfolio_value: 15_000,
    long_market_value: 5_000,
    short_market_value: 0,
    pattern_day_trader: false,
    trading_blocked: false,
    account_blocked: false,
    created_at_ms: 1_600_000_000_000,
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

function fakeClerkStatus(): ClerkStatus {
  return {
    account_id: TEST_ACCOUNT_ID,
    broker: 'alpaca',
    hold: { active: false },
    latest_reconciliation: { verdict: 'clean', recorded_at_ms: 1_700_000_000_000 },
    outstanding_intents: 0,
    observed_at_ms: 1_700_000_000_000,
    operator_posture: healthyAccountOperatorPostureFixture(),
  };
}

async function renderWorkspace(
  overrides: {
    url?: string;
    directory?: FleetDirectoryDouble;
    verdict?: LaneVerdictState;
    money?: () => Promise<AccountMoneyView>;
  } = {},
) {
  const directory = overrides.directory ?? provideFleetDirectory();
  const getAccount = vi.fn(() => Promise.resolve(fakeAccount()));

  const view = await render(WorkspaceHostComponent, {
    providers: [
      { provide: directory.provide, useValue: directory.useValue },
      provideRouter(
        WORKSPACE_ROUTES,
        withComponentInputBinding(),
        // The lane-scoped tabs read `:clerkId` from the shell's own route and
        // `surface` from their route data, neither of which reaches a
        // non-empty child without this (the app config sets the same).
        withRouterConfig({ paramsInheritanceStrategy: 'always' }),
      ),
      { provide: MessageService, useValue: { add: vi.fn() } },
      {
        provide: BrokersService,
        useValue: {
          getAccount,
          getClerkStatus: () => Promise.resolve(fakeClerkStatus()),
        },
      },
      // The header's figures are the account-money read's. `readDeskState`
      // is the account list's cards, not this workspace's: the account-list
      // route above renders them.
      {
        provide: BrokerV2PanelService,
        useValue: {
          getAccountMoney: (target: { accountId: string | null }) =>
            overrides.money?.() ?? Promise.resolve(fakeAccountMoney({ account_id: target.accountId ?? '' })),
        },
      },
      {
        provide: BrokerConfigurationService,
        useValue: { readDeskState: () => new Promise<never>(() => undefined) },
      },
      {
        provide: AlpacaLiveVerdictService,
        useValue: {
          stateFor: () => overrides.verdict ?? fakeVerdictState('paper'),
          start: vi.fn(),
        },
      },
    ],
  });

  const router = view.fixture.debugElement.injector.get(Router);
  await router.navigateByUrl(overrides.url ?? WORKSPACE_URL);
  await view.fixture.whenStable();
  return { view, router, getAccount };
}

describe('AlpacaAccountWorkspaceComponent', () => {
  it.each([
    [WORKSPACE_URL, 'Overview'],
    [`${WORKSPACE_URL}/bots`, 'Bots'],
    [`${WORKSPACE_URL}/gallery`, 'Gallery'],
    [`${WORKSPACE_URL}/deploy`, 'Deploy strategy'],
  ])('renders the account header and marks the open tab on %s', async (url, tab) => {
    await renderWorkspace({ url });

    expect(await screen.findByRole('heading', { name: 'Paper' })).toBeTruthy();
    expect(screen.getByText(`${tab} tab`)).toBeTruthy();
    for (const label of ['Overview', 'Bots', 'Gallery', 'Configuration', 'Deploy strategy']) {
      expect(screen.getByRole('link', { name: label })).toBeTruthy();
    }
    expect(screen.getByRole('link', { name: tab }).getAttribute('aria-current')).toBe('page');
    expect(
      screen.getAllByRole('link').filter((link) => link.getAttribute('aria-current') === 'page'),
    ).toHaveLength(1);
  });

  it('hosts the Bots tab’s roster commands, including Flatten cohort, while it is registered', async () => {
    const { view } = await renderWorkspace({ url: `${WORKSPACE_URL}/bots` });
    const bridge = view.fixture.debugElement.injector.get(BotsPageActionsBridgeService);
    const host = {
      refreshing: signal(false),
      initialLoading: signal(false),
      refresh: vi.fn(),
      openArchive: vi.fn(),
      openCohortFlatten: vi.fn(),
    };
    expect(screen.queryByRole('button', { name: 'Flatten cohort' })).toBeNull();

    bridge.register(host);
    view.fixture.detectChanges();
    fireEvent.click(await screen.findByRole('button', { name: 'Flatten cohort' }));

    expect(host.openCohortFlatten).toHaveBeenCalledTimes(1);
    expect(host.openArchive).not.toHaveBeenCalled();
  });

  it('has no detectable accessibility violations', async () => {
    await renderWorkspace();
    await screen.findByRole('heading', { name: 'Paper' });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations).toEqual([]);
  });

  it('points the Configuration tab at the lane’s own configuration page', async () => {
    await renderWorkspace();

    expect(screen.getByRole('link', { name: 'Configuration' }).getAttribute('href')).toBe(
      `${LANE_URL}/configuration`,
    );
  });

  describe('the lane-scoped tabs', () => {
    it('renders Configuration inside the workspace, under this account’s header', async () => {
      // Configuration stays lane-scoped (FR-092) but is no longer a page of
      // its own: the account header and the tab strip frame it like any tab.
      await renderWorkspace({ url: `${LANE_URL}/configuration` });

      expect(await screen.findByRole('heading', { name: 'Paper' })).toBeTruthy();
      expect(screen.getByText('Configuration tab')).toBeTruthy();
      expect(screen.getByRole('link', { name: 'Configuration' }).getAttribute('aria-current')).toBe(
        'page',
      );
      // A confirmed lane reads its account's own facts there, exactly as the
      // account-scoped tabs do (FR-092: "from the lane's confirmed account").
      expect(await screen.findByText('$98,329.57')).toBeTruthy();
    });

    it('keeps the workspace for a lane Alpaca has confirmed no account for', async () => {
      await renderWorkspace({
        url: `${LANE_URL}/configuration`,
        directory: unboundDirectory(),
      });

      // The lane's own label stands in for the account name it has not got.
      expect(await screen.findByRole('heading', { name: 'Unbound' })).toBeTruthy();
      expect(screen.getByText('Configuration tab')).toBeTruthy();
      // Equity and a sync verdict belong to an account. Saying "$—" and
      // "Not reconciled" here would report a failed read where there was none.
      expect(screen.getByText('No confirmed account')).toBeTruthy();
      expect(screen.queryByText(/Equity/)).toBeNull();
      expect(screen.queryByText(/Free to deploy/)).toBeNull();
    });

    it('offers no Overview to a lane with no account, rather than another lane’s', async () => {
      await renderWorkspace({
        url: `${LANE_URL}/configuration`,
        directory: unboundDirectory(),
      });

      await screen.findByRole('heading', { name: 'Unbound' });
      expect(screen.queryByRole('link', { name: 'Overview' })).toBeNull();
      expect(screen.getByText('Overview').getAttribute('aria-disabled')).toBe('true');
      expect(screen.getByRole('link', { name: 'Configuration' })).toBeTruthy();
    });

    it('has no detectable accessibility violations with no account to offer', async () => {
      // The bound render above never emits the inert Overview tab, so this is
      // the only pass that grades it — and the unoffered tab is exactly the
      // markup an operator is most likely to meet with a screen reader.
      await renderWorkspace({ url: `${LANE_URL}/configuration`, directory: unboundDirectory() });
      await screen.findByRole('heading', { name: 'Unbound' });

      const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

      expect(results.violations).toEqual([]);
    });

    it.each([
      ['bots', 'Bots roster'],
      ['gallery', 'Gallery'],
    ] as const)(
      'explains in place why the %s tab cannot open, and links to Configuration',
      async (surface, surfaceName) => {
        const { router } = await renderWorkspace({
          url: `${LANE_URL}/${surface}`,
          directory: unboundDirectory(),
        });

        expect(await screen.findByText(`${surfaceName} unavailable`)).toBeTruthy();
        expect(screen.getByText(/no confirmed account binding yet/i)).toBeTruthy();
        expect(
          screen.getByRole('link', { name: 'Open lane configuration' }).getAttribute('href'),
        ).toBe(`${LANE_URL}/configuration`);
        // FR-096: it fails in place. No other lane, and no other account, is
        // substituted by the navigation itself.
        expect(router.url).toBe(`${LANE_URL}/${surface}`);
      },
    );
  });

  it('renders Free to deploy, Cash, Equity and Today exactly as the money read authored them', async () => {
    await renderWorkspace();

    const figure = async (term: string) =>
      (await screen.findByText(term, { selector: 'dt' })).nextElementSibling?.textContent?.trim();
    expect(await figure('Free to deploy')).toBe('$98,329.57');
    expect(await figure('Cash')).toBe('$98,564.86');
    expect(await figure('Equity')).toBe('$100,012.40');
    expect(await figure('Today')).toBe('-$3.20');
  });

  it('carries no sync indicator — an out-of-sync account is a Home attention line', async () => {
    await renderWorkspace();
    await screen.findByText('Free to deploy');

    expect(screen.queryByText(/^Sync/)).toBeNull();
    expect(screen.queryByText('Clean')).toBeNull();
  });

  it('says why the money cannot be read, in the backend’s words, never $0', async () => {
    await renderWorkspace({
      money: () => Promise.resolve({
        ...unavailableAccountMoney('No daily loss limit is set for this account, so new entries are refused. Set one in Settings.'),
        account_id: TEST_ACCOUNT_ID,
      }),
    });

    expect(await screen.findByText(/No daily loss limit is set for this account/)).toBeTruthy();
    expect(screen.queryByText('Free to deploy')).toBeNull();
    expect(screen.queryByText(/\$0\.00/)).toBeNull();
  });

  it('keeps Alpaca’s Equity and Today, dated, when the bar cannot be drawn', async () => {
    await renderWorkspace({
      money: () => Promise.resolve({
        ...unavailableAccountMoney('A manual order is still working, so this account’s money cannot be drawn yet.'),
        account_id: TEST_ACCOUNT_ID,
        equity_usd: '100012.40',
        today_pnl_usd: '-3.20',
        observed_at_ms: 1_700_000_000_000,
      }),
    });

    const figure = async (term: string) =>
      (await screen.findByText(term, { selector: 'dt' })).nextElementSibling?.textContent?.trim();
    expect(await figure('Equity')).toBe('$100,012.40');
    expect(await figure('Today')).toBe('-$3.20');
    const readAt = (await screen.findByText('Read at', { selector: 'dt' })).nextElementSibling;
    expect(readAt?.querySelector('app-timestamp-display')?.textContent?.trim()).toBe(
      formatTimestampDisplay(1_700_000_000_000, { mode: 'local' }),
    );
    expect(screen.getByText(/A manual order is still working/)).toBeTruthy();
    expect(screen.queryByText('Free to deploy')).toBeNull();
    expect(screen.queryByText('Cash', { selector: 'dt' })).toBeNull();
  });

  it('says a failed money read failed rather than showing nothing', async () => {
    await renderWorkspace({ money: () => Promise.reject(new Error('money read failed')) });

    expect(await screen.findByText(/Account money could not be read\./)).toBeTruthy();
  });

  it('names the next step a refused money read authored, instead of discarding it', async () => {
    const refusal = new HttpErrorResponse({
      status: 503,
      error: {
        detail: {
          message: 'This account’s money cannot be read right now.',
          why: 'The account’s records are still being opened.',
          next_action: 'Open the account’s Settings to see why, then retry.',
        },
      },
    });
    await renderWorkspace({ money: () => Promise.reject(refusal) });

    const reason = await screen.findByText(/cannot be read right now/);
    expect(reason.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'This account’s money cannot be read right now. The account’s records are still being opened. '
        + 'Open the account’s Settings to see why, then retry.',
    );
  });

  it.each([
    ['paper', fakeVerdictState('paper'), 'PAPER · practice money'],
    ['live', fakeVerdictState('live', {}), 'LIVE · real money'],
    ['shadow', fakeVerdictState('shadow', { clerk_authority: 'shadow' }), 'SHADOW · simulated fills on your live account'],
  ])('words the %s mode the one way the server verdict declares', async (_label, verdict, mode) => {
    await renderWorkspace({ verdict });

    expect(await screen.findByText(mode)).toBeTruthy();
    expect(screen.queryByText(/armed/)).toBeNull();
  });

  it('reads a cold load as reading the mode, never as a real-money warning (H4)', async () => {
    await renderWorkspace({ verdict: UNPOLLED_LANE_STATE });

    expect(await screen.findByText('Reading account mode…')).toBeTruthy();
    expect(screen.queryByText(/assume real money/)).toBeNull();
  });

  it('keeps the fail-closed wording when the mode read failed', async () => {
    await renderWorkspace({ verdict: { verdict: null, lastError: new Error('verdict read failed') } });

    expect(await screen.findByText('Mode unknown — assume real money')).toBeTruthy();
  });

  it('points Deploy at the workspace’s own lane and account from every other tab', async () => {
    // Blocked-reason messaging (no lane, no declared capability, no confirmed
    // account) is `AlpacaDeployTabComponent`'s own concern now — this shell
    // only has to get the tab's address right, from wherever it is clicked.
    await renderWorkspace({ url: `${WORKSPACE_URL}/gallery` });

    expect(screen.getByRole('link', { name: 'Deploy strategy' }).getAttribute('href')).toBe(
      `${WORKSPACE_URL}/deploy`,
    );
  });

  it('lands on the chosen account’s Deploy tab under a deploy intent', async () => {
    // The regression this pins: the link's `href` stayed correct while the
    // destination stopped reading `?deploy`, so choosing an account under a
    // deploy intent landed on Overview with nothing open. This follows the
    // card and looks at what the destination actually renders, not just its
    // href. The list has no Deploy link of its own (#2187): the whole card is
    // the click target, and it carries the intent the hand-off arrived with.
    const { router } = await renderWorkspace({ url: `${ACCOUNT_LIST_URL}?deploy=` });

    fireEvent.click(await screen.findByRole('link', { name: /Paper/ }));

    await vi.waitFor(() => expect(router.url).toBe(`${WORKSPACE_URL}/deploy`));
    expect(await screen.findByText('Deploy strategy tab')).toBeTruthy();
  });

  describe("a bot's own page", () => {
    it.each([
      ['?from=gallery', 'Gallery', 'the Gallery it was opened from'],
      ['?from=bots', 'Bots', 'the roster it was opened from'],
      ['', 'Bots', 'Bots, because a pasted URL carries no stamp'],
      ['?from=elsewhere', 'Bots', 'Bots, because the stamp is not a tab'],
    ])('renders inside the workspace and highlights %s → %s', async (query, tab) => {
      await renderWorkspace({ url: `${WORKSPACE_URL}/bots/sid-1${query}` });

      expect(await screen.findByRole('heading', { name: 'Paper' })).toBeTruthy();
      expect(screen.getByText('Bot page')).toBeTruthy();
      expect(screen.getByRole('link', { name: tab }).getAttribute('aria-current')).toBe('page');
      expect(
        screen.getAllByRole('link').filter((link) => link.getAttribute('aria-current') === 'page'),
      ).toHaveLength(1);
    });
  });

  describe('where the keyboard goes', () => {
    function workspaceBody(): HTMLElement {
      const body = document.querySelector<HTMLElement>('.account-workspace__body');
      if (body === null) throw new Error('The workspace body is not rendered.');
      return body;
    }

    it('does not take focus on arrival', async () => {
      // The operator may already be somewhere — the address bar, a link they
      // opened this page from. Rendering is not a reason to move them.
      await renderWorkspace();
      await screen.findByRole('heading', { name: 'Paper' });

      expect(document.activeElement).not.toBe(workspaceBody());
    });

    it.each([
      [`${WORKSPACE_URL}/gallery`, 'a tab change'],
      [`${WORKSPACE_URL}/bots/sid-1`, "a bot's page opening"],
    ])('moves into the tab body after %s', async (url) => {
      // The header and tab strip do not move, so without this the keyboard
      // stays on the link just followed while everything below it changed.
      const { view, router } = await renderWorkspace();
      await screen.findByRole('heading', { name: 'Paper' });

      await router.navigateByUrl(url);
      await view.fixture.whenStable();

      await vi.waitFor(() => expect(document.activeElement).toBe(workspaceBody()));
    });

    it('moves into the tab body after an account switch', async () => {
      const { view, router } = await renderWorkspace({
        directory: provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [testLane(), testLane({ clerk_id: 'clrk_live', display_label: 'Live' })],
        }),
      });
      await screen.findByRole('heading', { name: 'Paper' });

      await router.navigateByUrl('/brokers/alpaca/clerks/clrk_live/configuration');
      await view.fixture.whenStable();

      await vi.waitFor(() => expect(document.activeElement).toBe(workspaceBody()));
    });

    it('does not steal focus when the fleet directory resolves the account after arrival on a lane-scoped tab', async () => {
      // The directory starts without this clerk's lane at all — the same
      // "not loaded yet" shape `lane()` reports while `/api/broker-clerks` is
      // in flight — so the header opens on "Lane unresolved" exactly as it
      // does while the real request is outstanding.
      const directory = provideFleetDirectory({ observed_at_ms: 1, clerks: [] });
      const { view } = await renderWorkspace({ url: `${LANE_URL}/configuration`, directory });
      await screen.findByText('Lane unresolved');

      // The directory now resolves this lane's confirmed account — a tick or
      // two after first render, never something the operator did. The URL
      // has not moved, so focus must not either.
      directory.rebind({ observed_at_ms: 2, clerks: [testLane()] });
      await directory.useValue.refresh?.();
      await view.fixture.whenStable();
      await screen.findByText('$98,329.57');

      expect(document.activeElement).not.toBe(workspaceBody());
    });

    it('moves focus when a not-ready tab’s roster link resolves to the real, servable roster', async () => {
      // The lane's account is already confirmed when this renders — the
      // mirror-image case: the resolved account id never changes, but the
      // whole body swaps from the refusal to the real roster, and that swap
      // is what must move the keyboard.
      const { view } = await renderWorkspace({ url: `${LANE_URL}/bots` });
      const rosterLink = await screen.findByRole('link', { name: 'Open the Bots roster' });

      fireEvent.click(rosterLink);
      await view.fixture.whenStable();

      await vi.waitFor(() => expect(document.activeElement).toBe(workspaceBody()));
    });
  });

  it('keeps one workspace — and one account read — across a tab change', async () => {
    const { view, router, getAccount } = await renderWorkspace();
    const headerBefore = await screen.findByRole('heading', { name: 'Paper' });

    await router.navigateByUrl(`${WORKSPACE_URL}/bots`);
    await view.fixture.whenStable();
    await router.navigateByUrl(`${WORKSPACE_URL}/gallery`);
    await view.fixture.whenStable();

    expect(screen.getByText('Gallery tab')).toBeTruthy();
    // The same DOM node, not an equal one: a re-created workspace would
    // flicker its header and drop every per-lane read behind it.
    expect(screen.getByRole('heading', { name: 'Paper' })).toBe(headerBefore);
    expect(getAccount).toHaveBeenCalledTimes(1);
  });

  describe('the account switcher', () => {
    const LIVE_CLERK_ID = 'clrk_live';
    const LIVE_ACCOUNT_ID = 'acct-live';
    const LIVE_URL = `/brokers/alpaca/clerks/${LIVE_CLERK_ID}/accounts/${LIVE_ACCOUNT_ID}`;

    /** Paper and Live side by side — the fleet an operator actually switches
     * between. */
    function twoLaneDirectory() {
      return provideFleetDirectory({
        observed_at_ms: 1,
        clerks: [
          testLane(),
          testLane({
            clerk_id: LIVE_CLERK_ID,
            display_label: 'Live',
            provider_summary: {
              ...testLane().provider_summary,
              confirmed_account_id: LIVE_ACCOUNT_ID,
            },
          }),
        ],
      });
    }

    async function openSwitcher(url: string) {
      const rendered = await renderWorkspace({ url, directory: twoLaneDirectory() });
      fireEvent.click(await screen.findByRole('button', { name: /Paper/ }));
      return rendered;
    }

    it('lists every Alpaca account by name, with its mode beside the name', async () => {
      await openSwitcher(WORKSPACE_URL);

      // ADR 0064 Decision 5: the name and the Paper/Live mode are two facts,
      // never folded into one string.
      const live = screen.getByRole('link', { name: /^Live/ });
      expect(live.textContent).toContain('Live');
      expect(live.textContent).toContain('PAPER · practice money');
      expect(screen.getByRole('link', { name: /^Paper/ }).getAttribute('aria-current')).toBe('true');
    });

    it.each([
      [WORKSPACE_URL, LIVE_URL, 'Overview'],
      [`${WORKSPACE_URL}/bots`, `${LIVE_URL}/bots`, 'Bots'],
      [`${WORKSPACE_URL}/gallery`, `${LIVE_URL}/gallery`, 'Gallery'],
      [`${LANE_URL}/configuration`, `/brokers/alpaca/clerks/${LIVE_CLERK_ID}/configuration`, 'Configuration'],
      [`${WORKSPACE_URL}/deploy`, `${LIVE_URL}/deploy`, 'Deploy'],
    ])('lands on the same tab of the chosen account, from %s', async (url, expected) => {
      await openSwitcher(url);

      expect(screen.getByRole('link', { name: /^Live/ }).getAttribute('href')).toBe(expected);
    });

    it("lands on the chosen account's Bots from a bot's page", async () => {
      // The chosen account need not run this bot, so the bot's page itself is
      // never carried across (ADR 0064 Decision 4).
      await openSwitcher(`${WORKSPACE_URL}/bots/sid-1?from=gallery`);

      expect(screen.getByRole('link', { name: /^Live/ }).getAttribute('href')).toBe(
        `${LIVE_URL}/bots`,
      );
    });

    it('carries the lens perspective across', async () => {
      await openSwitcher(`${WORKSPACE_URL}/bots?lens=operator`);

      expect(screen.getByRole('link', { name: /^Live/ }).getAttribute('href')).toBe(
        `${LIVE_URL}/bots?lens=operator`,
      );
    });

    it('is keyboard operable, and hands the keyboard back when dismissed', async () => {
      await openSwitcher(WORKSPACE_URL);
      const trigger = screen.getByRole('button', { name: /Paper/ });
      expect(trigger.getAttribute('aria-expanded')).toBe('true');

      fireEvent.keyDown(screen.getByRole('link', { name: /^Live/ }), { key: 'Escape' });

      await vi.waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('false'));
      expect(document.activeElement).toBe(trigger);
    });

    it('closes on a click outside it', async () => {
      // The click-outside listener is attached while the list is open and torn
      // down with it, rather than sitting on the host for the whole session —
      // this pins that the dismissal still works from that shorter life.
      await openSwitcher(WORKSPACE_URL);
      const trigger = screen.getByRole('button', { name: /Paper/ });
      await vi.waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('true'));

      fireEvent.click(document.body);

      await vi.waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('false'));
    });

    it('has no detectable accessibility violations while open', async () => {
      await openSwitcher(WORKSPACE_URL);

      const results = await axe.run(document.body, {
        rules: { 'color-contrast': { enabled: false } },
      });

      expect(results.violations).toEqual([]);
    });
  });

  it('shows the lane label beside an account name another lane shares', async () => {
    // ADR 0064 Decision 5: nothing refuses the duplicate; each colliding lane
    // is shown with its own label so the two stay distinguishable.
    const directory = provideFleetDirectory({
      observed_at_ms: 1,
      clerks: [
        testLane({ provider_summary: { account_nickname: 'Growth' } }),
        testLane({
          clerk_id: 'clrk_other',
          display_label: 'Live',
          provider_summary: { account_nickname: 'Growth' },
        }),
      ],
    });

    await renderWorkspace({ directory });

    expect(await screen.findByRole('heading', { name: 'Growth (Paper)' })).toBeTruthy();
  });
});
