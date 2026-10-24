import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import { of } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { provideFleetDirectory, testLane, TEST_ACCOUNT_ID, TEST_CLERK_ID } from '../../../fleet/fleet-directory-testing';
import type { FleetDirectoryResponse } from '../../../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService } from '../../../services/alpaca-live-verdict.service';
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import { fakeVerdictState } from '../../../testing/alpaca-live-verdict-fixtures';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { GoldenSearchService } from '../../golden-search/golden-search.service';
import type { QualificationDeployOffer } from '../../golden-search/golden-search.types';
import { deployOffer as goldenDeployOffer } from '../../golden-search/testing/fixtures';
import { GoldenOptionSetupsService, type GoldenOptionSetup } from '../../options-backtest/golden-option-setups.service';
import { goldenSetup } from '../../options-backtest/testing/golden-fixture';
import { AlpacaAccountListPageComponent } from './alpaca-account-list-page.component';
import { BrokerConfigurationService } from './configuration/broker-configuration.service';

/**
 * The approved options version the account list carries to Deploy (`?options_setup=`, PRD #3490
 * R23, I02, T13): shown exactly as chosen while the owner picks an account, re-read while they
 * decide, and refused by name when it stops being ready, with no other version and no account
 * put in its place. The Deploy page applies its own checks to it again, in its own specs.
 */

const LIVE_CLERK_ID = 'clrk_live0000000000000000bb';
const LIVE_ACCOUNT_ID = 'live-account-0001';
const VERSION_ID = goldenSetup().id;

function twoAccounts(): FleetDirectoryResponse {
  return {
    observed_at_ms: 1,
    clerks: [
      testLane(),
      testLane({
        clerk_id: LIVE_CLERK_ID,
        display_label: 'Live',
        provider_summary: { ...testLane().provider_summary, confirmed_account_id: LIVE_ACCOUNT_ID },
      }),
    ],
  };
}

/** The list opened on a handoff's address, with the router standing on it as it does in the app. */
async function renderHandoff(
  query: Record<string, string>,
  get: (id: string) => Promise<GoldenOptionSetup>,
  deployOffer: (id: string) => Promise<QualificationDeployOffer> = () => Promise.reject(new Error('No signal version was expected to be read here.')),
) {
  const queryParamMap = convertToParamMap(query);
  const paramMap = convertToParamMap({});
  const view = await render(AlpacaAccountListPageComponent, {
    providers: [
      provideFleetDirectory(twoAccounts()),
      provideRouter([]),
      { provide: AlpacaLiveVerdictService, useValue: { stateFor: () => fakeVerdictState('paper') } },
      { provide: BrokerV2PanelService, useValue: { getAccountMoney: (target: { accountId?: string }) => Promise.resolve(fakeAccountMoney({ account_id: target.accountId ?? '' })) } },
      { provide: BrokerConfigurationService, useValue: { readDeskState: () => Promise.resolve({ headline: 'Not ready yet' }) } },
      // Anything but a read of the one version named would fail loudly.
      { provide: GoldenOptionSetupsService, useValue: { get } },
      { provide: GoldenSearchService, useValue: { deployOffer } },
      { provide: ActivatedRoute, useValue: { queryParamMap: of(queryParamMap), paramMap: of(paramMap), snapshot: { queryParamMap, paramMap } } },
    ],
  });
  await TestBed.inject(Router).navigateByUrl(`/?${new URLSearchParams(query).toString()}`);
  await view.fixture.whenStable();
  return view;
}

function versionAs(overrides: Partial<GoldenOptionSetup>) {
  return vi.fn(async (id: string) => goldenSetup({ id, ...overrides }));
}

function deployHref(clerkId: string, accountId: string, versionId: string): string {
  return `/brokers/alpaca/clerks/${clerkId}/accounts/${accountId}/deploy?options_setup=${versionId}`;
}

afterEach(() => {
  vi.useRealTimers();
});

describe('the account list, carrying an approved options version', () => {
  it('names the exact version, its symbol and its source run, and carries that one id to every account without choosing one', async () => {
    const get = versionAs({ name: 'Morning put spread' });
    await renderHandoff({ options_setup: VERSION_ID }, get);

    const note = await screen.findByRole('status', { name: 'Approved options setup' });
    expect(note.textContent).toContain(VERSION_ID);
    expect(note.textContent).toContain('Morning put spread');
    expect(note.textContent).toContain('SPY');
    expect(note.textContent).toContain('Approved from run 7.');
    expect(note.textContent).toContain('Nothing is deployed from this page.');
    // The deploy-a-strategy wording belongs to the signal handoff; it is not said for an options version.
    expect(screen.queryByText(/to deploy a strategy/)).toBeNull();

    const here = TestBed.inject(Router).url;
    expect(screen.getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual([
      deployHref(TEST_CLERK_ID, TEST_ACCOUNT_ID, VERSION_ID),
      deployHref(LIVE_CLERK_ID, LIVE_ACCOUNT_ID, VERSION_ID),
    ]);
    // Reading the version moved nobody: the account is the owner's click, and it was never made.
    expect(TestBed.inject(Router).url).toBe(here);
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith(VERSION_ID);
  });

  it('shows a version that is revoked while an account is being chosen, naming it, with no other version or account in its place', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    let revoked = false;
    const get = vi.fn(async (id: string) => goldenSetup({
      id,
      ...(revoked ? { status: 'revoked' as const, status_explanation: 'Revoked by the owner: it cannot be deployed or restored.' } : {}),
    }));
    await renderHandoff({ options_setup: VERSION_ID }, get);
    await screen.findByRole('status', { name: 'Approved options setup' });
    const here = TestBed.inject(Router).url;

    revoked = true;
    vi.advanceTimersByTime(15_000);

    const refusal = await screen.findByRole('alert');
    expect(refusal.textContent).toContain(`Approved options setup ${VERSION_ID} for SPY is revoked and cannot be deployed.`);
    expect(refusal.textContent).toContain('Revoked by the owner: it cannot be deployed or restored.');
    expect(refusal.textContent).toContain('No other version was chosen in its place and no account was chosen.');
    expect(screen.queryByRole('status', { name: 'Approved options setup' })).toBeNull();
    // The way on is the version's own record or the list of setups; nothing was picked for the owner.
    expect(within(refusal).getByRole('link', { name: 'Open this version' }).getAttribute('href')).toBe(`/options-setups/evidence/golden/${VERSION_ID}`);
    expect(within(refusal).getByRole('link', { name: 'Choose a version in Options setups' }).getAttribute('href')).toBe('/options-setups');
    expect(TestBed.inject(Router).url).toBe(here);
    expect(get.mock.calls.every(([id]) => id === VERSION_ID)).toBe(true);
    // The cards still name the version that was chosen, so Deploy refuses that one at its own boundary.
    for (const link of screen.getAllByRole('link', { name: /Paper|Live/ })) {
      expect(link.getAttribute('href')).toContain(`options_setup=${VERSION_ID}`);
    }
  });

  it('keeps a stale refusal until the owner asks again, and shows the version again only if that read says it is ready', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    let status: GoldenOptionSetup['status'] = 'stale';
    const get = vi.fn(async (id: string) => goldenSetup({ id, status, status_explanation: 'The rules changed since approval; re-prove it.' }));
    await renderHandoff({ options_setup: VERSION_ID }, get);
    const refusal = await screen.findByRole('alert');
    expect(refusal.textContent).toContain(`Approved options setup ${VERSION_ID} for SPY is stale and cannot be deployed.`);
    expect(refusal.textContent).toContain('The rules changed since approval; re-prove it.');

    // It was re-proved elsewhere, but the page does not quietly turn back on its own.
    status = 'ready';
    vi.advanceTimersByTime(60_000);
    expect(get).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('alert')).toBeTruthy();

    fireEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Check again' }));
    expect((await screen.findByRole('status', { name: 'Approved options setup' })).textContent).toContain(VERSION_ID);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('says a version that cannot be read could not be read, with the server’s words, and substitutes nothing', async () => {
    const missing = `ogs_${'c'.repeat(32)}`;
    const get = vi.fn(() => Promise.reject(new HttpErrorResponse({ status: 404, error: { detail: { message: `No golden option setup ${missing} exists.` } } })));
    await renderHandoff({ options_setup: missing }, get);

    const refusal = await screen.findByRole('alert');
    expect(refusal.textContent).toContain(`Approved options setup ${missing} could not be read.`);
    expect(refusal.textContent).toContain(`No golden option setup ${missing} exists.`);
    expect(refusal.textContent).toContain('Nothing was substituted and no account was chosen.');
    expect(within(refusal).getByRole('link', { name: 'Choose a version in Options setups' }).getAttribute('href')).toBe('/options-setups');
    for (const link of screen.getAllByRole('link', { name: /Paper|Live/ })) {
      expect(link.getAttribute('href')).toContain(`options_setup=${missing}`);
    }
  });

  it('refuses an answer about another version instead of showing it as the chosen one', async () => {
    const other = `ogs_${'d'.repeat(32)}`;
    await renderHandoff({ options_setup: VERSION_ID }, vi.fn(async () => goldenSetup({ id: other })));

    const refusal = await screen.findByRole('alert');
    expect(refusal.textContent).toContain(`The answer for approved options setup ${VERSION_ID} named a different one, ${other}.`);
    expect(refusal.textContent).toContain('Nothing was applied and no account was chosen.');
    expect(screen.queryByRole('status', { name: 'Approved options setup' })).toBeNull();
  });

  it.each([
    ['names no version', ''],
    ['names a signal qualification', 'gq-0001-aaaa-bbbb'],
    ['is not an id at all', '../accounts'],
  ])('refuses a link that %s without reading anything', async (_case, value) => {
    const get = vi.fn();
    await renderHandoff({ options_setup: value }, get);

    expect(await screen.findByText('This link does not name an approved options setup.')).toBeTruthy();
    expect(screen.queryByRole('status', { name: 'Approved options setup' })).toBeNull();
    expect(get).not.toHaveBeenCalled();
  });

  it('is not shown for a signal handoff, which reads no options version and is carried under its own keys', async () => {
    const get = vi.fn();
    const deployOffer = vi.fn(async (id: string) => goldenDeployOffer({ qualification_id: id }));
    await renderHandoff({ strategy: 'ema_crossover_signal', golden_qualification: 'gq-0001-aaaa-bbbb' }, get, deployOffer);

    expect((await screen.findByRole('status', { name: 'Golden configuration' })).textContent).toContain('Choose a ready Paper or Live account below to use the golden configuration.');
    expect(screen.queryByRole('status', { name: 'Approved options setup' })).toBeNull();
    for (const link of screen.getAllByRole('link', { name: /Paper|Live/ })) {
      expect(link.getAttribute('href')).toContain('golden_qualification=gq-0001-aaaa-bbbb');
      expect(link.getAttribute('href')).not.toContain('options_setup');
    }
    expect(get).not.toHaveBeenCalled();
    expect(deployOffer).toHaveBeenCalledExactlyOnceWith('gq-0001-aaaa-bbbb');
  });
});
