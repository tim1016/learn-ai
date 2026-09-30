import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component } from '@angular/core';
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
import { BrokerV2PanelService, type AccountMoneyView } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { AlpacaAccountListPageComponent } from '../alpaca-desk/alpaca-account-list-page.component';
import { BrokerConfigurationService } from '../alpaca-desk/configuration/broker-configuration.service';
import { AlpacaAccountWorkspaceComponent } from './alpaca-account-workspace.component';
import { AlpacaSurfaceNotReadyTabComponent } from './alpaca-surface-not-ready-tab.component';

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
@Component({ selector: 'app-home-stub', template: '<main aria-label="Home">Home tab</main>' })
class HomeStubComponent {}

@Component({ selector: 'app-activity-stub', template: '<main aria-label="Activity">Activity tab</main>' })
class ActivityStubComponent {}

@Component({ selector: 'app-history-stub', template: '<main aria-label="History">History tab</main>' })
class HistoryStubComponent {}

@Component({
  selector: 'app-settings-stub',
  template: '<main aria-label="Settings">Settings tab</main>',
})
class SettingsStubComponent {}

@Component({ selector: 'app-bot-stub', template: '<main aria-label="Bot">Bot page</main>' })
class BotStubComponent {}

@Component({
  selector: 'app-deploy-stub',
  template: '<main aria-label="Deploy a bot">Deploy a bot tab</main>',
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
      { path: 'settings', component: SettingsStubComponent },
      { path: 'history', component: HistoryStubComponent },
      { path: 'home', component: AlpacaSurfaceNotReadyTabComponent },
      {
        path: 'accounts/:accountId',
        children: [
          { path: 'bots/:sid', component: BotStubComponent },
          { path: 'activity', component: ActivityStubComponent },
          { path: 'deploy', component: DeployStubComponent },
          { path: '', component: HomeStubComponent },
        ],
      },
      { path: '', redirectTo: 'settings', pathMatch: 'full' },
    ],
  },
  // The real account list, so a spec can follow one of its account cards into
  // the workspace instead of only reading the card's `href`.
  { path: 'brokers/alpaca', component: AlpacaAccountListPageComponent },
];

/** A ready lane that Alpaca has not confirmed an account for: it keeps its
 * workspace, and only Settings can open (ADR 0064, FR-096). */
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
    [WORKSPACE_URL, 'Home'],
    [`${WORKSPACE_URL}?view=wall`, 'Home'],
    [`${WORKSPACE_URL}/activity`, 'Activity'],
    [`${LANE_URL}/history`, 'History'],
    [`${WORKSPACE_URL}/deploy`, 'Deploy a bot'],
  ])('renders the account header and marks the open tab on %s', async (url, tab) => {
    await renderWorkspace({ url });

    expect(await screen.findByRole('heading', { name: 'Paper' })).toBeTruthy();
    expect(screen.getByText(`${tab} tab`)).toBeTruthy();
    // Overview, Bots and Gallery are one Home tab (PRD #2560).
    for (const label of ['Home', 'Activity', 'History', 'Settings', 'Deploy a bot']) {
      expect(screen.getByRole('link', { name: label })).toBeTruthy();
    }
    for (const retired of ['Overview', 'Bots', 'Gallery']) {
      expect(screen.queryByRole('link', { name: retired })).toBeNull();
    }
    expect(screen.getByRole('link', { name: tab }).getAttribute('aria-current')).toBe('page');
    expect(
      screen.getAllByRole('link').filter((link) => link.getAttribute('aria-current') === 'page'),
    ).toHaveLength(1);
  });

  it('hosts no page commands in the header: Home carries its own', async () => {
    await renderWorkspace();
    await screen.findByRole('heading', { name: 'Paper' });

    for (const retired of ['Refresh bots', 'Archive finished', 'Flatten cohort']) {
      expect(screen.queryByRole('button', { name: retired })).toBeNull();
    }
  });

  it('has no detectable accessibility violations', async () => {
    await renderWorkspace();
    await screen.findByRole('heading', { name: 'Paper' });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations).toEqual([]);
  });

  it('points the Settings tab at the lane’s own Settings page', async () => {
    await renderWorkspace();

    expect(screen.getByRole('link', { name: 'Settings' }).getAttribute('href')).toBe(
      `${LANE_URL}/settings`,
    );
  });

  describe('the lane-scoped tabs', () => {
    it('renders Settings inside the workspace, under this account’s header', async () => {
      // Settings stays lane-scoped (FR-092) but is no longer a page of
      // its own: the account header and the tab strip frame it like any tab.
      await renderWorkspace({ url: `${LANE_URL}/settings` });

      expect(await screen.findByRole('heading', { name: 'Paper' })).toBeTruthy();
      expect(screen.getByText('Settings tab')).toBeTruthy();
      expect(screen.getByRole('link', { name: 'Settings' }).getAttribute('aria-current')).toBe(
        'page',
      );
      // A confirmed lane reads its account's own facts there, exactly as the
      // account-scoped tabs do (FR-092: "from the lane's confirmed account").
      expect(await screen.findByText('$98,329.57')).toBeTruthy();
    });

    it('keeps the workspace for a lane Alpaca has confirmed no account for', async () => {
      await renderWorkspace({
        url: `${LANE_URL}/settings`,
        directory: unboundDirectory(),
      });

      // The lane's own label stands in for the account name it has not got.
      expect(await screen.findByRole('heading', { name: 'Unbound' })).toBeTruthy();
      expect(screen.getByText('Settings tab')).toBeTruthy();
      // Equity and a sync verdict belong to an account. Saying "$—" and
      // "Not reconciled" here would report a failed read where there was none.
      expect(screen.getByText('No confirmed account')).toBeTruthy();
      expect(screen.queryByText(/Equity/)).toBeNull();
      expect(screen.queryByText(/Free to deploy/)).toBeNull();
    });

    it('offers no Deploy to a lane with no account, and Home explains itself in place', async () => {
      await renderWorkspace({
        url: `${LANE_URL}/settings`,
        directory: unboundDirectory(),
      });

      await screen.findByRole('heading', { name: 'Unbound' });
      // Deploy a bot is the account's own action: an accountless lane shows
      // no button at all, and Activity — the account's history — is inert.
      expect(screen.queryByRole('link', { name: 'Deploy a bot' })).toBeNull();
      expect(screen.queryByText('Deploy a bot')).toBeNull();
      expect(screen.getByText('Activity').getAttribute('aria-disabled')).toBe('true');
      expect(screen.getByRole('link', { name: 'Home' }).getAttribute('href')).toBe(`${LANE_URL}/home`);
      expect(screen.getByRole('link', { name: 'Settings' })).toBeTruthy();
    });

    it('has no detectable accessibility violations with no account to offer', async () => {
      // The bound render above never emits the inert Activity tab, so this is
      // the only pass that grades it — and the unoffered tab is exactly the
      // markup an operator is most likely to meet with a screen reader.
      await renderWorkspace({ url: `${LANE_URL}/settings`, directory: unboundDirectory() });
      await screen.findByRole('heading', { name: 'Unbound' });

      const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

      expect(results.violations).toEqual([]);
    });

    it('explains in place why Home cannot open, and links to Settings', async () => {
      const { router } = await renderWorkspace({
        url: `${LANE_URL}/home`,
        directory: unboundDirectory(),
      });

      expect(await screen.findByText('Home unavailable')).toBeTruthy();
      expect(screen.getByText(/no confirmed account binding yet/i)).toBeTruthy();
      expect(
        screen.getByRole('link', { name: 'Open Settings' }).getAttribute('href'),
      ).toBe(`${LANE_URL}/settings`);
      expect(screen.getByRole('link', { name: 'Home' }).getAttribute('aria-current')).toBe('page');
      // FR-096: it fails in place. No other lane, and no other account, is
      // substituted by the navigation itself.
      expect(router.url).toBe(`${LANE_URL}/home`);
    });
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
    // only has to get the button's address right, from wherever it is clicked.
    await renderWorkspace({ url: `${WORKSPACE_URL}/bots/sid-1` });

    expect(screen.getByRole('link', { name: 'Deploy a bot' }).getAttribute('href')).toBe(
      `${WORKSPACE_URL}/deploy`,
    );
  });

  it('offers Deploy as a header button, never as a tab in the strip (PRD #2560 D3)', async () => {
    await renderWorkspace({ url: `${WORKSPACE_URL}/deploy` });

    const strip = await screen.findByRole('navigation', { name: 'Account sections' });
    expect(within(strip).queryByRole('link', { name: /Deploy/ })).toBeNull();
    const deploy = screen.getByRole('link', { name: 'Deploy a bot' });
    expect(deploy.closest('header')).not.toBeNull();
    expect(deploy.getAttribute('aria-current')).toBe('page');
  });

  it('lands on the chosen account’s Deploy tab under a deploy intent', async () => {
    // The regression this pins: the link's `href` stayed correct while the
    // destination stopped reading `?deploy`, so choosing an account under a
    // deploy intent landed on the account's page with nothing open. This follows the
    // card and looks at what the destination actually renders, not just its
    // href. The list has no Deploy link of its own (#2187): the whole card is
    // the click target, and it carries the intent the hand-off arrived with.
    const { router } = await renderWorkspace({ url: `${ACCOUNT_LIST_URL}?deploy=` });

    fireEvent.click(await screen.findByRole('link', { name: /Paper/ }));

    await vi.waitFor(() => expect(router.url).toBe(`${WORKSPACE_URL}/deploy`));
    expect(await screen.findByText('Deploy a bot tab')).toBeTruthy();
  });

  describe("a bot's own page", () => {
    it.each([[''], ['?from=gallery']])('renders inside the workspace under Home (%s)', async (query) => {
      await renderWorkspace({ url: `${WORKSPACE_URL}/bots/sid-1${query}` });

      expect(await screen.findByRole('heading', { name: 'Paper' })).toBeTruthy();
      expect(screen.getByText('Bot page')).toBeTruthy();
      expect(screen.getByRole('link', { name: 'Home' }).getAttribute('aria-current')).toBe('page');
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
      [`${WORKSPACE_URL}/deploy`, 'a tab change'],
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

    it('moves into the tab body after a move to another account', async () => {
      const { view, router } = await renderWorkspace({
        directory: provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [testLane(), testLane({ clerk_id: 'clrk_live', display_label: 'Live' })],
        }),
      });
      await screen.findByRole('heading', { name: 'Paper' });

      await router.navigateByUrl('/brokers/alpaca/clerks/clrk_live/settings');
      await view.fixture.whenStable();

      await vi.waitFor(() => expect(document.activeElement).toBe(workspaceBody()));
    });

    it('does not steal focus when the fleet directory resolves the account after arrival on a lane-scoped tab', async () => {
      // The directory starts without this clerk's lane at all — the same
      // "not loaded yet" shape `lane()` reports while `/api/broker-clerks` is
      // in flight — so the header opens on "Lane unresolved" exactly as it
      // does while the real request is outstanding.
      const directory = provideFleetDirectory({ observed_at_ms: 1, clerks: [] });
      const { view } = await renderWorkspace({ url: `${LANE_URL}/settings`, directory });
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

    it('moves focus when a not-ready Home’s link resolves to the real, servable Home', async () => {
      // The lane's account is already confirmed when this renders — the
      // mirror-image case: the resolved account id never changes, but the
      // whole body swaps from the refusal to the real Home, and that swap
      // is what must move the keyboard.
      const { view } = await renderWorkspace({ url: `${LANE_URL}/home` });
      const rosterLink = await screen.findByRole('link', { name: 'Open Home' });

      fireEvent.click(rosterLink);
      await view.fixture.whenStable();

      await vi.waitFor(() => expect(document.activeElement).toBe(workspaceBody()));
    });
  });

  it('keeps one workspace — and one account read — across a tab change', async () => {
    const { view, router, getAccount } = await renderWorkspace();
    const headerBefore = await screen.findByRole('heading', { name: 'Paper' });

    await router.navigateByUrl(`${WORKSPACE_URL}/deploy`);
    await view.fixture.whenStable();
    await router.navigateByUrl(`${WORKSPACE_URL}?view=wall`);
    await view.fixture.whenStable();

    expect(screen.getByText('Home tab')).toBeTruthy();
    // The same DOM node, not an equal one: a re-created workspace would
    // flicker its header and drop every per-lane read behind it.
    expect(screen.getByRole('heading', { name: 'Paper' })).toBe(headerBefore);
    expect(getAccount).toHaveBeenCalledTimes(1);
  });

  describe('the lane frame (PRD #2560 D4)', () => {
    function frame(): HTMLElement {
      const element = document.querySelector<HTMLElement>('.account-workspace');
      if (element === null) throw new Error('the workspace frame did not render');
      return element;
    }

    it.each([
      ['live', fakeVerdictState('live'), 'LIVE · real money'],
      ['paper', fakeVerdictState('paper'), 'PAPER · practice money'],
      ['shadow', fakeVerdictState('shadow', { clerk_authority: 'shadow' }), 'SHADOW · simulated fills on your live account'],
    ])('frames the %s workspace in its lane colour, with the mode worded in its badge', async (lane, verdict, mode) => {
      await renderWorkspace({ verdict });

      // The colour is never the only carrier: the badge words the mode.
      expect(await screen.findByText(mode)).toBeTruthy();
      expect(frame().getAttribute('data-lane')).toBe(lane);
    });

    it('frames a cold load neutrally, never in a guessed lane colour', async () => {
      await renderWorkspace({ verdict: UNPOLLED_LANE_STATE });

      expect(await screen.findByText('Reading account mode…')).toBeTruthy();
      expect(frame().hasAttribute('data-lane')).toBe(false);
    });

    it('has no account dropdown: the top-bar pills switch accounts', async () => {
      await renderWorkspace({
        directory: provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [testLane(), testLane({ clerk_id: 'clrk_live', display_label: 'Live' })],
        }),
      });

      expect(await screen.findByRole('heading', { name: 'Paper' })).toBeTruthy();
      expect(screen.queryByRole('button', { name: /Paper/ })).toBeNull();
      expect(screen.queryByRole('link', { name: /^Live/ })).toBeNull();
    });

    it('has no detectable accessibility violations on the Live frame', async () => {
      await renderWorkspace({ verdict: fakeVerdictState('live') });
      await screen.findByText('LIVE · real money');

      const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

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
