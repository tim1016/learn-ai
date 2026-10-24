import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import {
  provideFleetDirectory,
  testLane,
  TEST_ACCOUNT_ID,
  TEST_CLERK_ID,
} from '../../../fleet/fleet-directory-testing';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import type { FleetDirectoryResponse } from '../../../fleet/fleet-directory.types';
import type { ResourceTarget } from '../../../fleet/resource-target';
import { AlpacaLiveVerdictService } from '../../../services/alpaca-live-verdict.service';
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import { fakeVerdictState } from '../../../testing/alpaca-live-verdict-fixtures';
import { BrokerV2PanelService, type AccountMoneyView } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { GoldenSearchService } from '../../golden-search/golden-search.service';
import type { QualificationDeployOffer } from '../../golden-search/golden-search.types';
import { deployOffer as goldenDeployOffer } from '../../golden-search/testing/fixtures';
import {
  ADD_ACCOUNT_RUNBOOK_URL,
  AlpacaAccountListPageComponent,
  DIRECTORY_RUNBOOK_URL,
} from './alpaca-account-list-page.component';
import { BrokerConfigurationService } from './configuration/broker-configuration.service';

const LIVE_CLERK_ID = 'clrk_live0000000000000000bb';
const LIVE_ACCOUNT_ID = 'live-account-0001';

interface ListDoubles {
  getAccountMoney?: (target: ResourceTarget) => Promise<AccountMoneyView>;
  /** The version's deploy offer, as Deploy reads it. A page that asks for one it was not given fails loudly. */
  deployOffer?: (id: string) => Promise<QualificationDeployOffer>;
}

async function renderList(
  query: Record<string, string> = {},
  directory?: FleetDirectoryResponse,
  doubles: ListDoubles = {},
) {
  const queryParamMap = convertToParamMap(query);
  const paramMap = convertToParamMap({});
  return render(AlpacaAccountListPageComponent, {
    providers: [
      provideFleetDirectory(directory),
      provideRouter([]),
      { provide: AlpacaLiveVerdictService, useValue: { stateFor: () => fakeVerdictState('paper') } },
      {
        provide: BrokerV2PanelService,
        useValue: {
          getAccountMoney:
            doubles.getAccountMoney
            ?? ((target: ResourceTarget) => Promise.resolve(fakeAccountMoney({ account_id: target.accountId ?? '' }))),
        },
      },
      {
        provide: BrokerConfigurationService,
        useValue: { readDeskState: () => Promise.resolve({ headline: 'Not ready yet' }) },
      },
      {
        provide: GoldenSearchService,
        useValue: {
          deployOffer: doubles.deployOffer ?? (() => Promise.reject(new Error('No version was expected to be read here.'))),
        },
      },
      {
        provide: ActivatedRoute,
        useValue: {
          queryParamMap: of(queryParamMap),
          paramMap: of(paramMap),
          snapshot: { queryParamMap, paramMap },
        },
      },
    ],
  });
}

/** A directory double in a given load state, for the page-level states. */
function directoryDouble(state: { error?: unknown; loading?: boolean; refresh?: () => Promise<unknown> }) {
  return {
    provide: FleetDirectoryService,
    useValue: {
      value: () => undefined,
      error: () => state.error,
      isLoading: () => state.loading ?? false,
      lanesOf: () => [],
      refresh: state.refresh ?? (() => Promise.reject(new Error('still down'))),
    },
  };
}

function routeDouble() {
  return {
    provide: ActivatedRoute,
    useValue: {
      queryParamMap: of(convertToParamMap({})),
      paramMap: of(convertToParamMap({})),
      snapshot: { queryParamMap: convertToParamMap({}), paramMap: convertToParamMap({}) },
    },
  };
}

/** Paper and Live side by side — the two-account shape every isolation
 * assertion below needs. */
function twoAccounts(): FleetDirectoryResponse {
  return {
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
  };
}

const VERSION_ID = 'gq-0001-aaaa-bbbb';

/** The list opened on a handoff's address, with the router standing on it as it does in the app. */
async function renderHandoff(
  query: Record<string, string>,
  deployOffer: (id: string) => Promise<QualificationDeployOffer>,
  directory: FleetDirectoryResponse = twoAccounts(),
) {
  const view = await renderList(query, directory, { deployOffer });
  await TestBed.inject(Router).navigateByUrl(`/?${new URLSearchParams(query).toString()}`);
  await view.fixture.whenStable();
  return view;
}

describe('AlpacaAccountListPageComponent', () => {
  it('lists every account under one heading', async () => {
    await renderList();

    // #2183: the "Broker desk" eyebrow line above the page heading is
    // retired — the single heading carries the eyebrow look itself, and
    // names the page as the menu does (PRD #3490 R01).
    // #2187: the list's own second heading is retired with it, so "Accounts"
    // is the only heading the page renders.
    expect(screen.getByRole('heading', { name: 'Accounts' })).toBeTruthy();
    expect(screen.queryByText('Broker desk')).toBeNull();
    expect(screen.getAllByRole('heading')).toHaveLength(1);
    expect(screen.queryByRole('heading', { name: /Choose an account/ })).toBeNull();
    expect(screen.getByRole('list', { name: 'Alpaca accounts' })).toBeTruthy();
    expect(screen.getByText('1 registered')).toBeTruthy();
  });

  it('gives each account exactly one way in', async () => {
    await renderList({}, twoAccounts());

    const links = screen.getAllByRole('link');
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}`,
      `/brokers/alpaca/clerks/${LIVE_CLERK_ID}/accounts/${LIVE_ACCOUNT_ID}`,
    ]);
  });

  it('makes a broker-wide deploy intent an explicit account choice', async () => {
    await renderList({ deploy: '' }, twoAccounts());

    expect(
      screen.getByText(/choose a ready Paper or Live account below to deploy a strategy/i),
    ).toBeTruthy();
    // The intent has no lane of its own, so it travels with whichever account
    // the operator picks (FR-096 — nothing is chosen for them), landing on
    // that account's own Deploy tab rather than a `?deploy=` overlay.
    for (const link of screen.getAllByRole('link')) {
      expect(link.getAttribute('href')).toContain('/deploy');
    }
  });

  it('reads no version for a strategy-only deploy intent, and says the account is the next choice', async () => {
    const deployOffer = vi.fn();
    await renderList({ deploy: '', strategy: 'deployment_validation' }, twoAccounts(), { deployOffer });

    expect(screen.getByText(/choose a ready Paper or Live account below to deploy a strategy/i)).toBeTruthy();
    expect(deployOffer).not.toHaveBeenCalled();
  });

  it('carries a golden configuration from Golden Search to the chosen account’s Deploy (#2696)', async () => {
    const deployOffer = vi.fn(async (id: string) => goldenDeployOffer({ qualification_id: id }));
    await renderHandoff({ golden_qualification: VERSION_ID }, deployOffer);

    expect(await screen.findByText(/choose a ready Paper or Live account below to use the golden configuration/i)).toBeTruthy();
    for (const link of screen.getAllByRole('link')) {
      expect(link.getAttribute('href')).toMatch(/\/deploy\?golden_qualification=gq-0001-aaaa-bbbb$/);
    }
  });

  it('says the directory itself is unavailable, with a retry and the runbook that fixes it', async () => {
    const refresh = vi.fn(() => Promise.reject(new Error('still down')));
    await render(AlpacaAccountListPageComponent, {
      providers: [provideRouter([]), directoryDouble({ error: new Error('directory down'), refresh }), routeDouble()],
    });

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Your accounts could not be loaded');
    // Plain words: the owner is not told about the fleet machinery behind it.
    expect(alert.textContent).not.toMatch(/fleet|directory/i);
    expect(screen.getByRole('link', { name: 'How to bring the account list back' }).getAttribute('href')).toBe(
      DIRECTORY_RUNBOOK_URL,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/They still could not be loaded\./)).toBeTruthy();
  });

  it('names the next step when no account is registered yet', async () => {
    await render(AlpacaAccountListPageComponent, {
      providers: [provideRouter([]), directoryDouble({}), routeDouble()],
    });

    expect(screen.getByText('No Alpaca accounts are registered yet.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Add an Alpaca account' }).getAttribute('href')).toBe(
      ADD_ACCOUNT_RUNBOOK_URL,
    );
  });

  it("keeps one account whole while another's money read fails (FR-093)", async () => {
    // Every card owns its own read, so a failure is that account's alone.
    const getAccountMoney = vi.fn((target: ResourceTarget) =>
      target.clerkId === LIVE_CLERK_ID
        ? Promise.reject(new Error('live money read failed'))
        : Promise.resolve(fakeAccountMoney({ account_id: target.accountId ?? '' })),
    );
    await renderList({}, twoAccounts(), { getAccountMoney });

    expect(await screen.findByText(/Account money could not be read\./)).toBeTruthy();
    // The healthy account is untouched by its sibling's outage.
    expect((await screen.findByText(/Account money/, { selector: '.lane-card__figure' })).textContent).toContain('$100,000.00');
    expect(screen.getAllByRole('link')).toHaveLength(2);
  });

  it('reads each account under its own lane identity, never a sibling’s', async () => {
    const getAccountMoney = vi.fn((target: ResourceTarget) =>
      Promise.resolve(fakeAccountMoney({ account_id: target.accountId ?? '' })),
    );
    await renderList({}, twoAccounts(), { getAccountMoney });

    await vi.waitFor(() => expect(getAccountMoney).toHaveBeenCalledTimes(2));
    expect(
      getAccountMoney.mock.calls.map(([target]) => [target.clerkId, target.accountId]),
    ).toEqual([
      [TEST_CLERK_ID, TEST_ACCOUNT_ID],
      [LIVE_CLERK_ID, LIVE_ACCOUNT_ID],
    ]);
  });

  it('has no detectable accessibility violations with its mini bars and counts', async () => {
    await renderList({}, twoAccounts());
    await screen.findAllByText(/Free to deploy/);

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations).toEqual([]);
  });
});

describe('AlpacaAccountListPageComponent — the version it carries to Deploy (PRD #3490 R19)', () => {
  const STRATEGY = 'ema_crossover_signal';
  const HANDOFF = { strategy: STRATEGY, golden_qualification: VERSION_ID };

  function offerAs(overrides: Partial<QualificationDeployOffer>) {
    return vi.fn(async (id: string) => goldenDeployOffer({ qualification_id: id, ...overrides }));
  }

  it('names the exact version, its symbol and its settings, and says when it is not the current default', async () => {
    await renderHandoff(HANDOFF, offerAs({ is_default: false }));

    const note = await screen.findByRole('status', { name: 'Golden configuration' });
    expect(note.textContent).toContain(VERSION_ID);
    expect(note.textContent).toContain(STRATEGY);
    expect(note.textContent).toContain('fast_period = 8 · gap = 0.15 · rsi_max = 72 · rsi_min = 48 · slow_period = 21');
    expect(note.textContent).toContain('It is not the current default for QQQ; this exact version is the one carried.');
  });

  it('carries the same exact version to every account, and chooses no account itself', async () => {
    const deployOffer = offerAs({});
    await renderHandoff(HANDOFF, deployOffer);
    await screen.findByRole('status', { name: 'Golden configuration' });
    const here = TestBed.inject(Router).url;

    const links = screen.getAllByRole('link');
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}/deploy?strategy=${STRATEGY}&golden_qualification=${VERSION_ID}`,
      `/brokers/alpaca/clerks/${LIVE_CLERK_ID}/accounts/${LIVE_ACCOUNT_ID}/deploy?strategy=${STRATEGY}&golden_qualification=${VERSION_ID}`,
    ]);
    // Reading the version moved nobody: the account is the owner's click, and it was never made.
    expect(TestBed.inject(Router).url).toBe(here);
    expect(deployOffer).toHaveBeenCalledTimes(1);
    expect(deployOffer).toHaveBeenCalledWith(VERSION_ID);
  });

  it('shows a version that is revoked while an account is being chosen, naming it, with no other version in its place', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    try {
      let revoked = false;
      const deployOffer = vi.fn(async (id: string) => goldenDeployOffer({
        qualification_id: id,
        ...(revoked ? { status: 'revoked' as const, explanation: 'Revoked by the owner: it cannot be deployed or restored.' } : {}),
      }));
      const view = await renderHandoff(HANDOFF, deployOffer);
      await screen.findByRole('status', { name: 'Golden configuration' });
      const here = TestBed.inject(Router).url;

      revoked = true;
      vi.advanceTimersByTime(15_000);

      const refusal = await screen.findByRole('alert');
      expect(refusal.textContent).toContain(`Golden configuration ${VERSION_ID} for QQQ is revoked and cannot be deployed.`);
      expect(refusal.textContent).toContain('Revoked by the owner: it cannot be deployed or restored.');
      expect(refusal.textContent).toContain('No other version was chosen in its place and no account was chosen.');
      expect(screen.queryByRole('status', { name: 'Golden configuration' })).toBeNull();
      // The way to a new, explicit choice is the strategy's own versions, and nothing was picked for the owner.
      expect(within(refusal).getByRole('link', { name: 'Choose a version in Golden' }).getAttribute('href'))
        .toBe(`/signal-strategies/${STRATEGY}/golden/versions`);
      expect(TestBed.inject(Router).url).toBe(here);
      expect(deployOffer.mock.calls.every(([id]) => id === VERSION_ID)).toBe(true);
      // The cards still name the version that was chosen, so Deploy refuses that one at its own boundary.
      await view.fixture.whenStable();
      for (const link of screen.getAllByRole('link', { name: /Paper|Live/ })) {
        expect(link.getAttribute('href')).toContain(`golden_qualification=${VERSION_ID}`);
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps a refusal until the owner asks again, and shows the version again only if that read says it is ready', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    try {
      let status: QualificationDeployOffer['status'] = 'stale';
      const deployOffer = vi.fn(async (id: string) => goldenDeployOffer({ qualification_id: id, status, explanation: 'Approved, but the program changed since — re-proof it.' }));
      await renderHandoff(HANDOFF, deployOffer);
      const refusal = await screen.findByRole('alert');
      expect(refusal.textContent).toContain(`Golden configuration ${VERSION_ID} for QQQ is stale and cannot be deployed.`);
      expect(refusal.textContent).toContain('Approved, but the program changed since — re-proof it.');

      // It was re-proved elsewhere, but the page does not quietly turn back on its own.
      status = 'ready';
      vi.advanceTimersByTime(60_000);
      expect(deployOffer).toHaveBeenCalledTimes(1);
      expect(screen.getByRole('alert')).toBeTruthy();

      fireEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Check again' }));
      expect((await screen.findByRole('status', { name: 'Golden configuration' })).textContent).toContain(VERSION_ID);
      expect(deployOffer).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('says a version that cannot be read could not be read, with the server’s words, and substitutes nothing', async () => {
    const deployOffer = vi.fn(() => Promise.reject(new HttpErrorResponse({ status: 404, error: { detail: { message: 'No golden qualification gq-gone exists.' } } })));
    await renderHandoff({ strategy: STRATEGY, golden_qualification: 'gq-gone' }, deployOffer);

    const refusal = await screen.findByRole('alert');
    expect(refusal.textContent).toContain('Golden configuration gq-gone could not be read.');
    expect(refusal.textContent).toContain('No golden qualification gq-gone exists.');
    expect(refusal.textContent).toContain('Nothing was substituted and no account was chosen.');
    expect(within(refusal).getByRole('link', { name: 'Choose a version in Golden' }).getAttribute('href'))
      .toBe(`/signal-strategies/${STRATEGY}/golden/versions`);
    for (const link of screen.getAllByRole('link', { name: /Paper|Live/ })) {
      expect(link.getAttribute('href')).toContain('golden_qualification=gq-gone');
    }
  });

  it('refuses an answer about another version instead of showing it as the chosen one', async () => {
    await renderHandoff(HANDOFF, vi.fn(async () => goldenDeployOffer({ qualification_id: 'gq-9999-other' })));

    const refusal = await screen.findByRole('alert');
    expect(refusal.textContent).toContain(`The answer for golden configuration ${VERSION_ID} named a different one, gq-9999-other.`);
    expect(refusal.textContent).toContain('Nothing was applied and no account was chosen.');
    expect(screen.queryByRole('status', { name: 'Golden configuration' })).toBeNull();
  });

  it('refuses a link that names no version without reading anything', async () => {
    const deployOffer = vi.fn();
    await renderHandoff({ strategy: STRATEGY, golden_qualification: '' }, deployOffer);

    expect(await screen.findByText('This link does not name a golden configuration.')).toBeTruthy();
    expect(screen.queryByRole('status', { name: 'Golden configuration' })).toBeNull();
    expect(deployOffer).not.toHaveBeenCalled();
  });

  it('sends the owner back to the strategy list when the address names no strategy to return to', async () => {
    await renderHandoff({ golden_qualification: 'gq-gone' }, vi.fn(() => Promise.reject(new Error('down'))));

    const refusal = await screen.findByRole('alert');
    expect(within(refusal).getByRole('link', { name: 'Choose a strategy' }).getAttribute('href')).toBe('/signal-strategies');
  });
});
