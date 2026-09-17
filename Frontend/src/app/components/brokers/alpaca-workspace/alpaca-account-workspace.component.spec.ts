import { ChangeDetectionStrategy, Component } from '@angular/core';
import { Router, RouterOutlet, provideRouter, type Routes } from '@angular/router';
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

const WORKSPACE_URL = `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}`;
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

const WORKSPACE_ROUTES: Routes = [
  {
    path: 'brokers/alpaca/clerks/:clerkId/accounts/:accountId',
    component: AlpacaAccountWorkspaceComponent,
    children: [
      { path: 'bots', component: BotsStubComponent },
      { path: 'gallery', component: GalleryStubComponent },
      { path: '', component: OverviewStubComponent },
    ],
  },
  // The real account list, so a spec can follow its per-account links into
  // the workspace instead of only reading their `href`.
  { path: 'brokers/alpaca', component: AlpacaLaneDirectoryComponent },
];

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
      provideRouter(WORKSPACE_ROUTES),
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
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/configuration`,
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
