import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, type Routes } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { fireEvent, screen, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AlpacaAccountListPageComponent } from '../../components/brokers/alpaca-desk/alpaca-account-list-page.component';
import { BrokerConfigurationService } from '../../components/brokers/alpaca-desk/configuration/broker-configuration.service';
import { BrokerV2PanelService } from '../../components/broker/v2-panel/lib/broker-v2-panel.service';
import type { GoldenQualificationSummary, QualificationDeployOffer } from '../../components/golden-search/golden-search.types';
import { deployOffer as goldenDeployOffer } from '../../components/golden-search/testing/fixtures';
import { provideFleetDirectory, testLane, TEST_ACCOUNT_ID } from '../../fleet/fleet-directory-testing';
import { AlpacaLiveVerdictService } from '../../services/alpaca-live-verdict.service';
import { fakeAccountMoney } from '../../testing/account-money-fixtures';
import { fakeVerdictState } from '../../testing/alpaca-live-verdict-fixtures';
import { harnessEntry, qualification, registryEntry } from '../testing/fixtures';
import { emptyWorld, provideSignalStrategyWorld } from '../testing/signal-strategy-world';

/**
 * T13 (PRD #3490): a deploy handoff from a strategy's header, through the account
 * list, to an account's Deploy address, walked across the real pages with fakes for
 * everything they read. The Deploy page itself applies and refuses a version in its
 * own specs; what is proved here is that the exact intent reaches it, survives the
 * owner's choosing and re-choosing an account, and is refused, not replaced, when
 * the version is revoked on the way.
 */

const EMA = 'ema_crossover_signal';
const LIVE_CLERK_ID = 'clrk_live0000000000000000bb';
const LIVE_ACCOUNT_ID = 'live-account-0001';
const LIST_PATH = '/brokers/alpaca';

/** Each version's exact settings, as the list of versions and Deploy's offer both carry them. */
const SETTINGS: Readonly<Record<string, QualificationDeployOffer['parameters']>> = {
  'spy-default': { fast_period: 5, slow_period: 10 },
  'spy-earlier': { fast_period: 6, slow_period: 12 },
};

const DEFAULT_VERSION = qualification({ id: 'spy-default', symbol: 'SPY', status: 'ready', is_default: true, created_at_ms: 4, parameters: SETTINGS['spy-default'] });
const EARLIER_VERSION = qualification({ id: 'spy-earlier', symbol: 'SPY', status: 'ready', created_at_ms: 3, parameters: SETTINGS['spy-earlier'] });

/** What Deploy would be handed at an account's address, shown so a spec can read it. */
@Component({
  selector: 'app-deploy-address-stub',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<p role="status">Deploy on {{ accountId() }}: strategy {{ strategy() ?? 'none' }}, version {{ golden_qualification() ?? 'none' }}</p>`,
})
class DeployAddressStubComponent {
  readonly accountId = input<string>();
  readonly strategy = input<string>();
  readonly golden_qualification = input<string>();
}

const ROUTES: Routes = [
  { path: 'brokers/alpaca', component: AlpacaAccountListPageComponent },
  { path: 'brokers/alpaca/clerks/:clerkId/accounts/:accountId/deploy', component: DeployAddressStubComponent },
];

interface Walk {
  readonly harness: RouterTestingHarness;
  readonly http: HttpTestingController;
  readonly deployOffer: ReturnType<typeof vi.fn>;
  readonly getAccountMoney: ReturnType<typeof vi.fn>;
  /** A version the server now says is revoked. */
  revoke(id: string): void;
}

function offerOf(version: GoldenQualificationSummary, revoked: ReadonlySet<string>): QualificationDeployOffer {
  const isRevoked = revoked.has(version.id);
  return goldenDeployOffer({
    qualification_id: version.id,
    program_key: version.program_key,
    program_version: version.program_version,
    symbol: version.symbol,
    parameters: SETTINGS[version.id] ?? {},
    is_default: version.is_default,
    status: isRevoked ? 'revoked' : version.status,
    explanation: isRevoked ? 'Revoked by the owner: it cannot be deployed or restored.' : version.status_explanation,
  });
}

async function start(url: string, versions: readonly GoldenQualificationSummary[], entries = [registryEntry(), harnessEntry()]): Promise<Walk> {
  const revoked = new Set<string>();
  const deployOffer = vi.fn(async (id: string) => {
    const version = versions.find((candidate) => candidate.id === id);
    if (version === undefined) throw new Error(`No version ${id} exists.`);
    return offerOf(version, revoked);
  });
  const getAccountMoney = vi.fn(async (target: { accountId?: string }) => fakeAccountMoney({ account_id: target.accountId ?? '' }));
  const directory = provideFleetDirectory({
    observed_at_ms: 1,
    clerks: [
      testLane(),
      testLane({
        clerk_id: LIVE_CLERK_ID,
        display_label: 'Live',
        provider_summary: { ...testLane().provider_summary, confirmed_account_id: LIVE_ACCOUNT_ID },
      }),
    ],
  });
  TestBed.configureTestingModule({
    providers: [
      ...provideSignalStrategyWorld(emptyWorld({ entries, qualifications: versions }), {}, { golden: { deployOffer } }, ROUTES),
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: directory.provide, useValue: directory.useValue },
      { provide: AlpacaLiveVerdictService, useValue: { stateFor: () => fakeVerdictState('paper') } },
      { provide: BrokerV2PanelService, useValue: { getAccountMoney } },
      { provide: BrokerConfigurationService, useValue: { readDeskState: () => Promise.resolve({ headline: 'Not ready yet' }) } },
    ],
  });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  return { harness, http: TestBed.inject(HttpTestingController), deployOffer, getAccountMoney, revoke: (id) => revoked.add(id) };
}

/** The header's "Deploy a bot", opened. */
async function openDeployChoice(): Promise<HTMLElement> {
  fireEvent.click(await screen.findByRole('button', { name: 'Deploy a bot' }));
  return screen.findByRole('region', { name: 'Deploy a bot' });
}

function cardFor(account: 'Paper' | 'Live'): HTMLElement {
  return screen.getByRole('link', { name: new RegExp(account) });
}

afterEach(() => {
  vi.useRealTimers();
  TestBed.resetTestingModule();
});

describe('Deploy a bot from a strategy to an account’s Deploy (T13)', () => {
  it('opens Deploy with the exact version the owner chose, through the owner’s own choice of account', async () => {
    await start(`/signal-strategies/${EMA}/overview`, [DEFAULT_VERSION, EARLIER_VERSION]);
    const choice = await openDeployChoice();

    // The version that is not the default: its own id travels, not what the default would be.
    fireEvent.click(within(choice).getByRole('link', { name: 'Deploy version spy-earlier for SPY' }));

    const note = await screen.findByRole('status', { name: 'Golden configuration' });
    expect(TestBed.inject(Router).url).toBe(`${LIST_PATH}?strategy=${EMA}&golden_qualification=spy-earlier`);
    expect(note.textContent).toContain('spy-earlier');
    expect(note.textContent).toContain('fast_period = 6 · slow_period = 12');
    // Both accounts are offered and neither was taken.
    expect(screen.getAllByRole('link', { name: /Paper|Live/ })).toHaveLength(2);

    fireEvent.click(cardFor('Live'));
    expect(await screen.findByText(`Deploy on ${LIVE_ACCOUNT_ID}: strategy ${EMA}, version spy-earlier`)).toBeTruthy();
    expect(TestBed.inject(Router).url).toBe(
      `${LIST_PATH}/clerks/${LIVE_CLERK_ID}/accounts/${LIVE_ACCOUNT_ID}/deploy?strategy=${EMA}&golden_qualification=spy-earlier`,
    );
  });

  it('keeps the version a Deploy was opened for when the owner goes back and chooses another account', async () => {
    const { harness } = await start(`/signal-strategies/${EMA}/overview`, [DEFAULT_VERSION, EARLIER_VERSION]);
    fireEvent.click(within(await openDeployChoice()).getByRole('link', { name: 'Use the current default spy-default for SPY' }));
    await screen.findByRole('status', { name: 'Golden configuration' });
    const list = TestBed.inject(Router).url;

    fireEvent.click(cardFor('Paper'));
    expect(await screen.findByText(`Deploy on ${TEST_ACCOUNT_ID}: strategy ${EMA}, version spy-default`)).toBeTruthy();

    // Back to choosing: the address still names the version, so the other account is offered the same one.
    await harness.navigateByUrl(list);
    const note = await screen.findByRole('status', { name: 'Golden configuration' });
    expect(note.textContent).toContain('spy-default');
    expect(cardFor('Live').getAttribute('href')).toContain('golden_qualification=spy-default');
    expect(TestBed.inject(Router).url).toBe(list);

    fireEvent.click(cardFor('Live'));
    expect(await screen.findByText(`Deploy on ${LIVE_ACCOUNT_ID}: strategy ${EMA}, version spy-default`)).toBeTruthy();
  });

  it('refuses a version revoked while the owner chooses an account, naming it, and never puts the default or another account in its place', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const walk = await start(`/signal-strategies/${EMA}/overview`, [DEFAULT_VERSION, EARLIER_VERSION]);
    fireEvent.click(within(await openDeployChoice()).getByRole('link', { name: 'Deploy version spy-earlier for SPY' }));
    await screen.findByRole('status', { name: 'Golden configuration' });
    const list = TestBed.inject(Router).url;

    walk.revoke('spy-earlier');
    vi.advanceTimersByTime(15_000);

    const refusal = await screen.findByRole('alert');
    expect(refusal.textContent).toContain('Golden configuration spy-earlier for SPY is revoked and cannot be deployed.');
    expect(refusal.textContent).toContain('No other version was chosen in its place and no account was chosen.');
    expect(refusal.textContent).not.toContain('spy-default');
    // The default is ready, and is never asked for, linked or carried instead.
    expect(walk.deployOffer.mock.calls.every(([id]) => id === 'spy-earlier')).toBe(true);
    for (const link of screen.getAllByRole('link', { name: /Paper|Live/ })) {
      expect(link.getAttribute('href')).toContain('golden_qualification=spy-earlier');
      expect(link.getAttribute('href')).not.toContain('spy-default');
    }
    expect(TestBed.inject(Router).url).toBe(list);
    expect(within(refusal).getByRole('link', { name: 'Choose a version in Golden' }).getAttribute('href'))
      .toBe(`/signal-strategies/${EMA}/golden/versions`);
  });

  it('still deploys a strategy with no saved version at its registered point, reading no version', async () => {
    const walk = await start('/signal-strategies/deployment_validation/overview', []);
    const choice = await openDeployChoice();

    fireEvent.click(within(choice).getByRole('link', { name: 'Choose an account for deployment_validation without a saved version' }));

    expect(await screen.findByText(/choose a ready Paper or Live account below to deploy a strategy/i)).toBeTruthy();
    expect(TestBed.inject(Router).url).toBe(`${LIST_PATH}?deploy=&strategy=deployment_validation`);
    fireEvent.click(cardFor('Paper'));
    expect(await screen.findByText(`Deploy on ${TEST_ACCOUNT_ID}: strategy deployment_validation, version none`)).toBeTruthy();
    expect(walk.deployOffer).not.toHaveBeenCalled();
  });

  it('sends no request but reads, from the header to an account’s Deploy address', async () => {
    const walk = await start(`/signal-strategies/${EMA}/overview`, [DEFAULT_VERSION, EARLIER_VERSION]);
    fireEvent.click(within(await openDeployChoice()).getByRole('link', { name: 'Deploy version spy-earlier for SPY' }));
    await screen.findByRole('status', { name: 'Golden configuration' });
    fireEvent.click(cardFor('Paper'));
    await screen.findByText(`Deploy on ${TEST_ACCOUNT_ID}: strategy ${EMA}, version spy-earlier`);

    // The fakes hold only reads, so a page that reached for a command would have thrown.
    expect(walk.getAccountMoney).toHaveBeenCalled();
    const sent = walk.http.match(() => true).map((request) => request.request.method);
    expect(sent.filter((method) => method !== 'GET')).toEqual([]);
  });
});
