import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, type Routes } from '@angular/router';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AlpacaDeployTabComponent } from '../../../components/brokers/alpaca-workspace/alpaca-deploy-tab.component';
import { AlpacaAccountListPageComponent } from '../../../components/brokers/alpaca-desk/alpaca-account-list-page.component';
import { AlpacaDeskAccountDataService } from '../../../components/brokers/alpaca-desk/alpaca-desk-account-data.service';
import { BrokerConfigurationService } from '../../../components/brokers/alpaca-desk/configuration/broker-configuration.service';
import { DEPLOY_VIEW } from '../../../components/broker/broker-deploy-page/alpaca-deploy-workflow.fixtures';
import { OPTION_CATALOGUE } from '../../../components/broker/broker-deploy-page/option-structure/option-structure.fixtures';
import { OptionsBotDeployService } from '../../../components/broker/broker-deploy-page/options-bot-deploy.service';
import { BrokerV2PanelService, type BotEndPreviewRequest } from '../../../components/broker/v2-panel/lib/broker-v2-panel.service';
import type { GoldenOptionSetup } from '../../../components/options-backtest/golden-option-setups.service';
import { exampleBody } from '../../../components/options-backtest/testing/fixtures';
import { provideFleetDirectory, testLane, TEST_ACCOUNT_ID } from '../../../fleet/fleet-directory-testing';
import { resourceTarget, type ResourceTarget } from '../../../fleet/resource-target';
import { AlpacaLiveVerdictService } from '../../../services/alpaca-live-verdict.service';
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import { fakeVerdictState } from '../../../testing/alpaca-live-verdict-fixtures';
import { libraryWorld } from '../../testing/explore-actions';
import { approvalOf, indexOf, savedName } from '../../testing/fixtures';
import { versionOf } from '../../testing/golden-fixtures';
import { renderTab } from '../../testing/render-workspace';

/**
 * T13 (PRD #3490): a deploy handoff from an options setup's header, through the account list, to
 * an account's Deploy, walked across the real pages with fakes for everything they read. Options
 * Deploy is the real page: what is proved here is that the exact approved version reaches it,
 * survives the owner's choosing and re-choosing an account, and is refused, not replaced, when
 * it is revoked on the way. Deploy's own admission, eligibility and submission are not touched
 * and are proved in its own specs; every command in these fakes would throw if it were reached.
 */

const LIVE_CLERK_ID = 'clrk_live0000000000000000bb';
const LIVE_ACCOUNT_ID = 'live-account-0001';
const LIST_PATH = '/brokers/alpaca';
const OLDER = `ogs_${'a'.repeat(32)}`;
const NEWER = `ogs_${'b'.repeat(32)}`;
const END_MS = Date.UTC(2026, 9, 20, 19, 15);

const PLACES: Routes = [
  { path: 'brokers/alpaca', component: AlpacaAccountListPageComponent },
  { path: 'brokers/alpaca/clerks/:clerkId/accounts/:accountId/deploy', component: AlpacaDeployTabComponent },
];

interface Walk {
  /** Each version read by its own id, as the server answers it now. */
  readonly get: ReturnType<typeof vi.fn>;
  readonly review: ReturnType<typeof vi.fn>;
  readonly deploy: ReturnType<typeof vi.fn>;
  readonly submission: ReturnType<typeof vi.fn>;
  readonly http: HttpTestingController;
  /** A version the server now says is revoked. */
  revoke(id: string): void;
}

const targets = new Map<string, ResourceTarget>();

/** The account and clerk Deploy's address names, which is what the workspace shell hands the tab. */
function addressed(): { clerkId: string; accountId: string } {
  const found = /\/clerks\/([^/]+)\/accounts\/([^/?]+)/.exec(TestBed.inject(Router).url);
  return { clerkId: found?.[1] ?? '', accountId: found?.[2] ?? '' };
}

function accountDataAtTheAddress() {
  return {
    target: () => {
      const { clerkId, accountId } = addressed();
      const key = `${clerkId}/${accountId}`;
      const known = targets.get(key) ?? resourceTarget('alpaca', clerkId, { accountId, bindingGeneration: 3, routingEpoch: 4 });
      targets.set(key, known);
      return known;
    },
    accountId: () => addressed().accountId,
    fence: () => ({ bindingGeneration: 3, routingEpoch: 4 }),
    account: { hasValue: () => true },
    money: { isLoading: () => false, reload: () => true },
    reviewCurrentLane: () => undefined,
  };
}

/** Mine, an older and a newer approved version of its content, the account list and Deploy, with every read faked. */
async function start(url = 'overview'): Promise<Walk> {
  const revoked = new Set<string>();
  const versions: GoldenOptionSetup[] = [
    versionOf(NEWER, '1', { name: 'Afternoon call spread', source_run_id: 9 }),
    versionOf(OLDER, '1', { name: 'Morning put spread', source_run_id: 7 }),
  ];
  const asServed = (version: GoldenOptionSetup): GoldenOptionSetup => revoked.has(version.id)
    ? { ...version, status: 'revoked', status_explanation: 'Revoked by the owner: it cannot be deployed or restored.' }
    : version;
  const get = vi.fn(async (id: string) => {
    const found = versions.find((version) => version.id === id);
    if (found === undefined) throw new Error(`No version ${id} exists.`);
    return asServed(found);
  });
  const list = vi.fn(async () => versions.map(asServed));
  const review = vi.fn(() => Promise.reject(new Error('A review was sent.')));
  const deploy = vi.fn(() => Promise.reject(new Error('A deployment was sent.')));
  const submission = vi.fn(() => Promise.reject(new Error('A submission was read.')));
  const previewBotEnd = vi.fn(async (_target: ResourceTarget, body: BotEndPreviewRequest) => ({
    ...DEPLOY_VIEW.default_end, ...(body.end ?? { end_at_ms: END_MS, end_action: 'SELL' }), status: 'scheduled', headline: 'Reviewed bot end',
  }));
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

  const library = libraryWorld(vi);
  library.library.set('Mine', exampleBody());
  await renderTab(library.service, 'Mine', url as 'overview', {
    saved: [savedName('Mine', '1')],
    index: async () => indexOf({
      approvals: [
        approvalOf('1', { id: OLDER, source_run_id: 7, created_at_ms: 1_791_900_000_000 }),
        approvalOf('1', { id: NEWER, source_run_id: 9, created_at_ms: 1_791_900_100_000 }),
      ],
    }),
    bots: { kind: 'known', bots: [] },
    golden: { get, list },
    evidence: { read: vi.fn() },
    places: PLACES,
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: directory.provide, useValue: directory.useValue },
      { provide: AlpacaLiveVerdictService, useValue: { stateFor: () => fakeVerdictState('paper') } },
      {
        provide: BrokerV2PanelService,
        useValue: {
          getAccountMoney: (target: ResourceTarget) => Promise.resolve(fakeAccountMoney({ account_id: target.accountId ?? '' })),
          getDeployView: async () => DEPLOY_VIEW,
          getOptionStructures: async () => OPTION_CATALOGUE,
          previewBotEnd,
        },
      },
      { provide: BrokerConfigurationService, useValue: { readDeskState: () => Promise.resolve({ headline: 'Not ready yet' }) } },
      { provide: AlpacaDeskAccountDataService, useValue: accountDataAtTheAddress() },
      { provide: OptionsBotDeployService, useValue: { review, deploy, submission } },
    ],
  });
  return { get, review, deploy, submission, http: TestBed.inject(HttpTestingController), revoke: (id) => revoked.add(id) };
}

/** The header's "Deploy an options bot", opened. */
async function openDeployChoice(): Promise<HTMLElement> {
  fireEvent.click(await screen.findByRole('button', { name: 'Deploy an options bot' }));
  return screen.findByRole('region', { name: 'Deploy an options bot' });
}

/** Chooses a version by its own id and waits for the account list to name it. */
async function takeVersion(id: string): Promise<HTMLElement> {
  const choice = await openDeployChoice();
  fireEvent.click(await within(choice).findByRole('link', { name: `Deploy version ${id}` }));
  return screen.findByRole('status', { name: 'Approved options setup' });
}

function cardFor(account: 'Paper' | 'Live'): HTMLElement {
  return screen.getByRole('link', { name: new RegExp(account) });
}

const deployAddress = (clerkId: string, accountId: string, id: string) =>
  `${LIST_PATH}/clerks/${clerkId}/accounts/${accountId}/deploy?options_setup=${id}`;

/** What Deploy shows for the version it was opened on. */
async function deployedVersion(): Promise<HTMLSelectElement> {
  await screen.findByRole('heading', { name: 'Deploy an Options bot' });
  return screen.findByLabelText<HTMLSelectElement>('Approved tested version');
}

function deployedSelect(): HTMLSelectElement {
  return screen.getByLabelText<HTMLSelectElement>('Approved tested version');
}

afterEach(() => {
  vi.useRealTimers();
  targets.clear();
  TestBed.resetTestingModule();
});

describe('Deploy an options bot from a setup to an account’s Deploy (T13)', () => {
  it('opens Deploy with the exact version the owner chose, through the owner’s own choice of account', async () => {
    const walk = await start();

    // The older version: its own id travels, not the latest and not the first listed.
    const note = await takeVersion(OLDER);
    expect(TestBed.inject(Router).url).toBe(`${LIST_PATH}?options_setup=${OLDER}`);
    expect(note.textContent).toContain(OLDER);
    expect(note.textContent).toContain('Morning put spread');
    expect(note.textContent).not.toContain(NEWER);
    // Both accounts are offered and neither was taken.
    expect(screen.getAllByRole('link', { name: /Paper|Live/ })).toHaveLength(2);

    fireEvent.click(cardFor('Live'));

    const select = await deployedVersion();
    expect(TestBed.inject(Router).url).toBe(deployAddress(LIVE_CLERK_ID, LIVE_ACCOUNT_ID, OLDER));
    await waitFor(() => expect(select.value).toBe(OLDER));
    expect(screen.getByText(LIVE_ACCOUNT_ID)).toBeTruthy();
    // Deploy opened on the exact version and sent nothing: reviewing and deploying are the owner's later steps.
    expect(walk.review).not.toHaveBeenCalled();
    expect(walk.deploy).not.toHaveBeenCalled();
  });

  it('keeps the version a Deploy was opened for when the owner goes back and chooses another account', async () => {
    await start();
    await takeVersion(NEWER);
    const list = TestBed.inject(Router).url;

    fireEvent.click(cardFor('Paper'));
    await deployedVersion();
    await waitFor(() => expect(deployedSelect().value).toBe(NEWER));
    expect(screen.getByText(TEST_ACCOUNT_ID)).toBeTruthy();

    // Back to choosing: the address still names the version, so the other account is offered the same one.
    await TestBed.inject(Router).navigateByUrl(list);
    const note = await screen.findByRole('status', { name: 'Approved options setup' });
    expect(note.textContent).toContain(NEWER);
    expect(cardFor('Live').getAttribute('href')).toContain(`options_setup=${NEWER}`);
    expect(TestBed.inject(Router).url).toBe(list);

    fireEvent.click(cardFor('Live'));
    await deployedVersion();
    await waitFor(() => expect(deployedSelect().value).toBe(NEWER));
    expect(TestBed.inject(Router).url).toBe(deployAddress(LIVE_CLERK_ID, LIVE_ACCOUNT_ID, NEWER));
  });

  it('refuses a version revoked while the owner chooses an account, naming it, and never puts the other version or an account in its place', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const walk = await start();
    await takeVersion(OLDER);
    const list = TestBed.inject(Router).url;
    const readsBefore = walk.get.mock.calls.length;

    walk.revoke(OLDER);
    vi.advanceTimersByTime(15_000);

    const refusal = await screen.findByRole('alert');
    expect(refusal.textContent).toContain(`Approved options setup ${OLDER} for SPY is revoked and cannot be deployed.`);
    expect(refusal.textContent).toContain('No other version was chosen in its place and no account was chosen.');
    expect(refusal.textContent).not.toContain(NEWER);
    // The newer version is ready, and is never asked for, linked or carried instead.
    expect(walk.get.mock.calls.slice(readsBefore).every(([id]) => id === OLDER)).toBe(true);
    for (const link of screen.getAllByRole('link', { name: /Paper|Live/ })) {
      expect(link.getAttribute('href')).toContain(`options_setup=${OLDER}`);
      expect(link.getAttribute('href')).not.toContain(NEWER);
    }
    expect(TestBed.inject(Router).url).toBe(list);
    expect(within(refusal).getByRole('link', { name: 'Choose a version in Options setups' }).getAttribute('href')).toBe('/options-setups');

    // An owner who goes on anyway reaches Deploy, which names the same version and refuses it at its own boundary.
    fireEvent.click(cardFor('Paper'));
    const select = await deployedVersion();
    await waitFor(() => expect(select.value).toBe(OLDER));
    expect(screen.getByText('Revoked', { exact: true })).toBeTruthy();
    expect(screen.getByText('Revoked by the owner: it cannot be deployed or restored.')).toBeTruthy();
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Review setup and budget' }).disabled).toBe(true);
    expect(screen.getByText('Choose a ready approved setup before reviewing.')).toBeTruthy();
    expect(walk.review).not.toHaveBeenCalled();
    expect(walk.deploy).not.toHaveBeenCalled();
  });

  it('sends no request but reads, from the header to an account’s Deploy address', async () => {
    const walk = await start();
    await takeVersion(OLDER);
    fireEvent.click(cardFor('Paper'));
    await deployedVersion();
    await waitFor(() => expect(deployedSelect().value).toBe(OLDER));

    // The fakes hold only reads and would have thrown on a command; nothing reached the network layer either.
    expect(walk.review).not.toHaveBeenCalled();
    expect(walk.deploy).not.toHaveBeenCalled();
    expect(walk.submission).not.toHaveBeenCalled();
    const sent = walk.http.match(() => true).map((request) => request.request.method);
    expect(sent.filter((method) => method !== 'GET')).toEqual([]);
  });
});
