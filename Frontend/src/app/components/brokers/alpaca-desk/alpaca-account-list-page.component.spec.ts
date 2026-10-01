import { fireEvent, render, screen } from '@testing-library/angular';
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

describe('AlpacaAccountListPageComponent', () => {
  it('lists every account under one heading', async () => {
    await renderList();

    // #2183: the "Broker desk" eyebrow line above the page heading is
    // retired — the single "Alpaca" heading carries the eyebrow look itself.
    // #2187: the list's own second heading is retired with it, so "Alpaca"
    // is the only heading the page renders.
    expect(screen.getByRole('heading', { name: 'Alpaca' })).toBeTruthy();
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

  it('carries a golden configuration from Golden Search to the chosen account’s Deploy (#2696)', async () => {
    const view = await renderList({ golden_qualification: 'gq-0001-aaaa-bbbb' }, twoAccounts());
    await TestBed.inject(Router).navigateByUrl('/?golden_qualification=gq-0001-aaaa-bbbb');
    await view.fixture.whenStable();

    expect(screen.getByText(/choose a ready Paper or Live account below to use the golden configuration/i)).toBeTruthy();
    for (const link of screen.getAllByRole('link')) {
      expect(link.getAttribute('href')).toMatch(/\/deploy\?golden_qualification=gq-0001-aaaa-bbbb$/);
    }
  });

  it('retires the surface hints — a stale ?surface query renders the plain list', async () => {
    await renderList({ surface: 'bots' });

    expect(screen.getByRole('heading', { name: 'Alpaca' })).toBeTruthy();
    expect(screen.queryByText(/open its bots roster/i)).toBeNull();
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
