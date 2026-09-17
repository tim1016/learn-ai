import { render, screen } from '@testing-library/angular';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerAccountSnapshot } from '../../../api/alpaca.types';
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
import { BrokersService } from '../../../services/brokers.service';
import { fakeVerdictState } from '../../../testing/alpaca-live-verdict-fixtures';
import { fakeCatalogBot } from '../../../testing/bot-panel-fixtures';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { AlpacaAccountListPageComponent } from './alpaca-account-list-page.component';
import { BrokerConfigurationService } from './configuration/broker-configuration.service';

const LIVE_CLERK_ID = 'clrk_live0000000000000000bb';
const LIVE_ACCOUNT_ID = 'live-account-0001';

function fakeAccount(equity: number, accountId: string): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: accountId,
    account_mode: 'paper',
    account_status: 'ACTIVE',
    account_blocked: false,
    trading_blocked: false,
    pattern_day_trader: false,
    currency: 'USD',
    cash: equity,
    equity,
    buying_power: equity,
    portfolio_value: equity,
    long_market_value: 0,
    short_market_value: 0,
    created_at_ms: null,
    observed_at_ms: 1_757_000_000_000,
  } as BrokerAccountSnapshot;
}

interface ListDoubles {
  getAccount?: (target: ResourceTarget) => Promise<BrokerAccountSnapshot>;
  getCatalog?: (target: ResourceTarget) => Promise<ReturnType<typeof fakeCatalogBot>[]>;
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
      directory === undefined ? provideFleetDirectory() : provideFleetDirectory(directory),
      provideRouter([]),
      { provide: AlpacaLiveVerdictService, useValue: { stateFor: () => fakeVerdictState('paper') } },
      {
        provide: BrokersService,
        useValue: {
          getAccount:
            doubles.getAccount
            ?? ((target: ResourceTarget) =>
              Promise.resolve(fakeAccount(10_000, target.accountId ?? ''))),
        },
      },
      {
        provide: BrokerV2PanelService,
        useValue: { getCatalog: doubles.getCatalog ?? (() => Promise.resolve([fakeCatalogBot()])) },
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

  it('describes no lane mechanics and offers no surface chooser', async () => {
    await renderList({}, twoAccounts());

    // The paragraph that listed the card's six links, and the per-card
    // authority/binding/endpoint facts, are what #2187 removed: a list exists
    // to choose from, and the workspace holds an account's mechanics.
    expect(screen.queryByText(/Trader view for holdings/i)).toBeNull();
    expect(screen.queryByText(/no lane is selected automatically/i)).toBeNull();
    expect(screen.queryByText('Binding generation')).toBeNull();
    expect(screen.queryByText('Endpoint mode')).toBeNull();
  });

  it('makes a broker-wide deploy intent an explicit account choice', async () => {
    await renderList({ deploy: '' }, twoAccounts());

    expect(
      screen.getByText(/choose a ready Paper or Live account below to deploy a strategy/i),
    ).toBeTruthy();
    // The intent has no lane of its own, so it travels with whichever account
    // the operator picks (FR-096 — nothing is chosen for them).
    for (const link of screen.getAllByRole('link')) {
      expect(link.getAttribute('href')).toContain('?deploy=');
    }
  });

  it('retires the surface hints — a stale ?surface query renders the plain list', async () => {
    await renderList({ surface: 'bots' });

    expect(screen.getByRole('heading', { name: 'Alpaca' })).toBeTruthy();
    expect(screen.queryByText(/open its bots roster/i)).toBeNull();
  });

  it('says the directory itself is unavailable rather than showing an empty list', async () => {
    await render(AlpacaAccountListPageComponent, {
      providers: [
        provideRouter([]),
        {
          provide: FleetDirectoryService,
          useValue: {
            value: () => undefined,
            error: () => new Error('directory down'),
            isLoading: () => false,
            lanesOf: () => [],
          },
        },
        {
          provide: ActivatedRoute,
          useValue: {
            queryParamMap: of(convertToParamMap({})),
            paramMap: of(convertToParamMap({})),
            snapshot: { queryParamMap: convertToParamMap({}), paramMap: convertToParamMap({}) },
          },
        },
      ],
    });

    expect(screen.getByRole('alert').textContent).toContain('fleet directory is unavailable');
  });

  it("keeps one account whole while another's reads fail (FR-093)", async () => {
    // Every card owns its own reads, so a failure is that account's alone.
    // The proof is two accounts side by side: one card reporting what it
    // could not read while the other renders its money and its bots intact.
    const getAccount = vi.fn((target: ResourceTarget) =>
      target.clerkId === LIVE_CLERK_ID
        ? Promise.reject(new Error('live account read failed'))
        : Promise.resolve(fakeAccount(10_000, target.accountId ?? '')),
    );
    const getCatalog = vi.fn((target: ResourceTarget) =>
      target.clerkId === LIVE_CLERK_ID
        ? Promise.reject(new Error('live roster read failed'))
        : Promise.resolve([fakeCatalogBot()]),
    );
    await renderList({}, twoAccounts(), { getAccount, getCatalog });

    expect(await screen.findByText('Equity unavailable')).toBeTruthy();
    expect(await screen.findByText('Bot count unavailable')).toBeTruthy();
    // The healthy account is untouched by its sibling's outage.
    expect(await screen.findByText('$10,000.00')).toBeTruthy();
    expect(await screen.findByText('1 bot running')).toBeTruthy();
    expect(screen.getAllByRole('link')).toHaveLength(2);
  });

  it('reads each account under its own lane identity, never a sibling’s', async () => {
    const getAccount = vi.fn((target: ResourceTarget) =>
      Promise.resolve(fakeAccount(10_000, target.accountId ?? '')),
    );
    await renderList({}, twoAccounts(), { getAccount });

    await vi.waitFor(() => expect(getAccount).toHaveBeenCalledTimes(2));
    expect(
      getAccount.mock.calls.map(([target]) => [target.clerkId, target.accountId]),
    ).toEqual([
      [TEST_CLERK_ID, TEST_ACCOUNT_ID],
      [LIVE_CLERK_ID, LIVE_ACCOUNT_ID],
    ]);
  });
});
