import { ChangeDetectionStrategy, Component } from '@angular/core';
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
import type { ResourceTarget } from '../../../fleet/resource-target';
import { BrokersService } from '../../../services/brokers.service';
import {
  AlpacaLiveVerdictService,
  UNPOLLED_LANE_STATE,
  type LaneVerdictState,
} from '../../../services/alpaca-live-verdict.service';
import { fakeVerdictState } from '../../../testing/alpaca-live-verdict-fixtures';
import { healthyAccountOperatorPostureFixture } from '../../../testing/operator-blocker-fixtures';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { AlpacaLaneDirectoryComponent } from '../alpaca-desk/lane-directory/alpaca-lane-directory.component';
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
          { path: '', component: OverviewStubComponent },
        ],
      },
      { path: '', redirectTo: 'configuration', pathMatch: 'full' },
    ],
  },
  // The real account list, so a spec can follow its per-account links into
  // the workspace instead of only reading their `href`. Its own page supplies
  // these two inputs in the app; declared here because component input
  // binding sets every declared input from route data, `undefined` included.
  {
    path: 'brokers/alpaca',
    data: { surface: null, deployIntent: false },
    component: AlpacaLaneDirectoryComponent,
  },
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
    clerkStatusFails?: boolean;
  } = {},
) {
  const directory = overrides.directory ?? provideFleetDirectory();
  const getAccount = vi.fn(() => Promise.resolve(fakeAccount()));
  const getDeployView = vi.fn(
    (_target: ResourceTarget, _symbol?: string) => new Promise<never>(() => undefined),
  );

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
          getClerkStatus: () =>
            overrides.clerkStatusFails === true
              ? Promise.reject(new Error('clerk unavailable'))
              : Promise.resolve(fakeClerkStatus()),
        },
      },
      { provide: BrokerV2PanelService, useValue: { getDeployView } },
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
  return { view, router, getAccount, getDeployView };
}

describe('AlpacaAccountWorkspaceComponent', () => {
  it.each([
    [WORKSPACE_URL, 'Overview'],
    [`${WORKSPACE_URL}/bots`, 'Bots'],
    [`${WORKSPACE_URL}/gallery`, 'Gallery'],
  ])('renders the account header and marks the open tab on %s', async (url, tab) => {
    await renderWorkspace({ url });

    expect(await screen.findByRole('heading', { name: 'Paper' })).toBeTruthy();
    expect(screen.getByText(`${tab} tab`)).toBeTruthy();
    for (const label of ['Overview', 'Bots', 'Gallery', 'Configuration']) {
      expect(screen.getByRole('link', { name: label })).toBeTruthy();
    }
    expect(screen.getByRole('link', { name: tab }).getAttribute('aria-current')).toBe('page');
    expect(
      screen.getAllByRole('link').filter((link) => link.getAttribute('aria-current') === 'page'),
    ).toHaveLength(1);
  });

  // Declared before the Deploy tests below: those leave a PrimeNG drawer
  // appended to `document.body`, and this rule grades the whole document.
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
      expect(await screen.findByText(/\$15,000\.00/)).toBeTruthy();
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
      expect(screen.queryByText(/Reconciliation unavailable/)).toBeNull();
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

  it('renders the account’s equity and reconciliation verdict beside its name', async () => {
    await renderWorkspace();

    expect(await screen.findByText(/\$15,000\.00/)).toBeTruthy();
    // `clean` is a backend identifier, so it reaches the operator through the
    // shared receipt label, never raw.
    expect(screen.getByText('Clean')).toBeTruthy();
  });

  it('says the reconciliation read failed rather than showing nothing', async () => {
    await renderWorkspace({ clerkStatusFails: true });

    expect(await screen.findByText('Reconciliation unavailable')).toBeTruthy();
  });

  it.each([
    ['paper', fakeVerdictState('paper'), 'Paper money'],
    ['live-armed', fakeVerdictState('live-armed', { armed_instance_count: 2 }), 'Live'],
    ['unread', UNPOLLED_LANE_STATE, 'Mode unknown — assume real money'],
  ])('renders the mode chip the %s server verdict declares', async (_label, verdict, mode) => {
    await renderWorkspace({ verdict });

    expect(await screen.findByText(mode)).toBeTruthy();
  });

  it('names the armed count and the Shadow authority on a live lane', async () => {
    await renderWorkspace({
      verdict: fakeVerdictState('live-armed', {
        armed_instance_count: 2,
        clerk_authority: 'shadow',
      }),
    });

    expect(await screen.findByText('· 2 armed')).toBeTruthy();
    expect(screen.getByText('· Shadow')).toBeTruthy();
  });

  it('disables Deploy with its reason when the lane declares no deploy capability', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1,
      clerks: [testLane({ capabilities: ['account_read'] })],
    });
    const { getDeployView } = await renderWorkspace({ directory });

    const deploy = await screen.findByRole('button', { name: /Deploy strategy/ });
    expect(deploy.hasAttribute('disabled')).toBe(true);
    expect(screen.getByText('This clerk does not declare Deploy capability.')).toBeTruthy();
    expect(deploy.getAttribute('aria-describedby')).toBe('workspace-deploy-reason');

    fireEvent.click(deploy);

    expect(screen.queryByRole('heading', { name: 'Deploy a bot' })).toBeNull();
    expect(getDeployView).not.toHaveBeenCalled();
  });

  it('disables Deploy when the routed lane is not in the directory at all', async () => {
    const directory = provideFleetDirectory({ observed_at_ms: 1, clerks: [] });
    await renderWorkspace({ directory });

    expect(
      await screen.findByText(/lane has not resolved, so Deploy has no clerk to target/),
    ).toBeTruthy();
    expect(screen.getByRole('button', { name: /Deploy strategy/ }).hasAttribute('disabled')).toBe(
      true,
    );
  });

  it.each([
    [`${WORKSPACE_URL}/bots`],
    [`${WORKSPACE_URL}/gallery`],
  ])('opens Deploy from %s against the workspace lane and account', async (url) => {
    const { getDeployView, router } = await renderWorkspace({ url });

    fireEvent.click(await screen.findByRole('button', { name: /Deploy strategy/ }));

    expect(await screen.findByRole('heading', { name: 'Deploy a bot' })).toBeTruthy();
    // Opening writes `?deploy` on the tab the operator is standing on — the
    // drawer's address, which the menubar reads too. The tab itself does not
    // move.
    await vi.waitFor(() => expect(router.url).toBe(`${url}?deploy=`));
    // The workflow reads the target the drawer froze when it opened — never
    // the live directory while it is open (FR-094).
    await vi.waitFor(() => expect(getDeployView).toHaveBeenCalled());
    const [target] = getDeployView.mock.calls[0];
    expect(target.broker).toBe('alpaca');
    expect(target.clerkId).toBe(TEST_CLERK_ID);
    expect(target.accountId).toBe(TEST_ACCOUNT_ID);
    expect(target.bindingGeneration).toBe(3);
  });

  it('opens the Deploy drawer from a ?deploy deep link, and closing clears it', async () => {
    // `?deploy` is an address, not a private flag: the menubar's Deploy entry
    // and the strategy-validation hand-off both arrive by navigating to this
    // account with it set. Entered from another route, so the workspace is
    // created against that URL rather than reacting to a change on it.
    const { router } = await renderWorkspace({ url: ACCOUNT_LIST_URL });

    await router.navigateByUrl(`${WORKSPACE_URL}?deploy=`);

    expect(await screen.findByRole('heading', { name: 'Deploy a bot' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Close deploy a bot' }));

    await vi.waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Deploy a bot' })).toBeNull(),
    );
    await vi.waitFor(() => expect(router.url).not.toContain('deploy'));
  });

  it('opens the drawer when the account list’s own Deploy link is followed', async () => {
    // The regression this pins: the link's `href` stayed correct while the
    // destination stopped reading `?deploy`, so every per-account Deploy in
    // the account list navigated to Overview with nothing open. Asserting the
    // href alone could not see that — this follows the link and looks at what
    // the destination actually renders.
    await renderWorkspace({ url: ACCOUNT_LIST_URL });

    fireEvent.click(await screen.findByRole('link', { name: 'Deploy' }));

    expect(await screen.findByRole('heading', { name: 'Deploy a bot' })).toBeTruthy();
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
      await screen.findByText(/\$15,000\.00/);

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
      expect(live.textContent).toContain('Paper money');
      expect(screen.getByRole('link', { name: /^Paper/ }).getAttribute('aria-current')).toBe('true');
    });

    it.each([
      [WORKSPACE_URL, LIVE_URL, 'Overview'],
      [`${WORKSPACE_URL}/bots`, `${LIVE_URL}/bots`, 'Bots'],
      [`${WORKSPACE_URL}/gallery`, `${LIVE_URL}/gallery`, 'Gallery'],
      [`${LANE_URL}/configuration`, `/brokers/alpaca/clerks/${LIVE_CLERK_ID}/configuration`, 'Configuration'],
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

    it('closes an open Deploy drawer rather than retargeting it at the other account', async () => {
      const { router } = await renderWorkspace({
        url: ACCOUNT_LIST_URL,
        directory: twoLaneDirectory(),
      });
      await router.navigateByUrl(`${WORKSPACE_URL}?deploy=`);
      expect(await screen.findByRole('heading', { name: 'Deploy a bot' })).toBeTruthy();

      fireEvent.click(screen.getByRole('button', { name: /Paper/ }));
      fireEvent.click(screen.getByRole('link', { name: /^Live/ }));

      await vi.waitFor(() => expect(router.url).toBe(LIVE_URL));
      await vi.waitFor(() =>
        expect(screen.queryByRole('heading', { name: 'Deploy a bot' })).toBeNull(),
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
