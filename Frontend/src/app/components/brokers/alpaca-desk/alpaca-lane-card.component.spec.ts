import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerAccountSnapshot } from '../../../api/alpaca.types';
import {
  provideFleetDirectory,
  testLane,
  TEST_ACCOUNT_ID,
  TEST_CLERK_ID,
} from '../../../fleet/fleet-directory-testing';
import type { LaneDescriptor } from '../../../fleet/fleet-directory.types';
import type { ResourceTarget } from '../../../fleet/resource-target';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaLiveVerdictService } from '../../../services/alpaca-live-verdict.service';
import { fakeVerdictState } from '../../../testing/alpaca-live-verdict-fixtures';
import { fakeCatalogBot } from '../../../testing/bot-panel-fixtures';
import type { BotCatalogView } from '../../broker/v2-panel/lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { AlpacaLaneCardComponent } from './alpaca-lane-card.component';
import { BrokerConfigurationService } from './configuration/broker-configuration.service';

const WORKSPACE_URL = `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}`;
const CONFIGURATION_URL = `/brokers/alpaca/clerks/${TEST_CLERK_ID}/configuration`;
const OFFLINE_ACCOUNT_ID = 'live-account';
const OFFLINE_WORKSPACE_URL = `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${OFFLINE_ACCOUNT_ID}`;

function fakeAccount(overrides: Partial<BrokerAccountSnapshot> = {}): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: TEST_ACCOUNT_ID,
    account_mode: 'paper',
    account_status: 'ACTIVE',
    account_blocked: false,
    trading_blocked: false,
    pattern_day_trader: false,
    currency: 'USD',
    cash: 5_000,
    equity: 12_345.67,
    buying_power: 10_000,
    portfolio_value: 12_345.67,
    long_market_value: 7_345.67,
    short_market_value: 0,
    created_at_ms: null,
    observed_at_ms: 1_757_000_000_000,
    ...overrides,
  } as BrokerAccountSnapshot;
}

interface CardDoubles {
  getAccount?: (target: ResourceTarget) => Promise<BrokerAccountSnapshot>;
  getCatalog?: (target: ResourceTarget) => Promise<BotCatalogView[]>;
  readDeskState?: (clerkId: string) => Promise<{ headline: string }>;
}

async function renderCard(
  lane: LaneDescriptor = testLane(),
  doubles: CardDoubles = {},
  deployIntent = false,
  siblings: LaneDescriptor[] = [],
) {
  const getAccount = vi.fn(doubles.getAccount ?? (() => Promise.resolve(fakeAccount())));
  const getCatalog = vi.fn(doubles.getCatalog ?? (() => Promise.resolve([fakeCatalogBot()])));
  const readDeskState = vi.fn(
    doubles.readDeskState
      ?? (() => Promise.resolve({ headline: 'Select an Alpaca account to activate this lane' })),
  );
  const view = await render(AlpacaLaneCardComponent, {
    inputs: { lane, deployIntent },
    providers: [
      provideRouter([]),
      provideFleetDirectory({ observed_at_ms: 1, clerks: [lane, ...siblings] }),
      { provide: AlpacaLiveVerdictService, useValue: { stateFor: () => fakeVerdictState('paper') } },
      { provide: BrokersService, useValue: { getAccount } },
      { provide: BrokerV2PanelService, useValue: { getCatalog } },
      { provide: BrokerConfigurationService, useValue: { readDeskState } },
    ],
  });
  return { view, getAccount, getCatalog, readDeskState };
}

/** A ready lane Alpaca has confirmed no account for: it has no money and no
 * roster to show, so it reads like any other not-ready account here. */
const UNBOUND_LANE = testLane({
  display_label: 'Unbound',
  provider_summary: { ...testLane().provider_summary, confirmed_account_id: null },
});

/** A lane that is down but still carries the account Alpaca last confirmed
 * on it — the ordinary clerk-outage shape, not an unbound lane. */
const OFFLINE_LANE = testLane({
  display_label: 'Live',
  lifecycle_state: 'unreachable',
  provider_summary: { ...testLane().provider_summary, confirmed_account_id: OFFLINE_ACCOUNT_ID },
});

describe('AlpacaLaneCardComponent', () => {
  it('shows the account, its mode, its money and its running bots', async () => {
    await renderCard();

    expect(screen.getByText('Paper')).toBeTruthy();
    expect(screen.getByText('Paper money')).toBeTruthy();
    expect(await screen.findByText('$12,345.67')).toBeTruthy();
    expect(await screen.findByText('1 bot running')).toBeTruthy();
  });

  it('counts only the running bots on the account', async () => {
    await renderCard(testLane(), {
      getCatalog: () =>
        Promise.resolve([
          fakeCatalogBot({ strategy_instance_id: 'a', running: true }),
          fakeCatalogBot({ strategy_instance_id: 'b', running: true }),
          fakeCatalogBot({ strategy_instance_id: 'c', running: false }),
        ]),
    });

    expect(await screen.findByText('2 bots running')).toBeTruthy();
    expect(screen.queryByText('3 bots running')).toBeNull();
  });

  it('says so plainly when nothing is running', async () => {
    await renderCard(testLane(), { getCatalog: () => Promise.resolve([]) });

    expect(await screen.findByText('No bots running')).toBeTruthy();
  });

  it('is one click target into the account workspace, not a menu of links', async () => {
    await renderCard();

    const links = screen.getAllByRole('link');
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute('href')).toBe(WORKSPACE_URL);
  });

  it('shows no lane mechanics — those belong to the workspace, not to choosing', async () => {
    await renderCard();

    for (const mechanic of ['Endpoint mode', 'Authority', 'Binding generation', 'Account']) {
      expect(screen.queryByText(mechanic)).toBeNull();
    }
    // The raw confirmed account id was the card's own `<dd>` before #2187.
    expect(screen.queryByText(TEST_ACCOUNT_ID)).toBeNull();
    expect(screen.queryByText('Real Paper')).toBeNull();
  });

  it('opens a lane with no confirmed account on Configuration, with the readiness line the server authored', async () => {
    await renderCard(UNBOUND_LANE);

    expect(
      await screen.findByText('Select an Alpaca account to activate this lane'),
    ).toBeTruthy();
    expect(screen.getByRole('link').getAttribute('href')).toBe(CONFIGURATION_URL);
    // Money and a bot count belong to an account; this lane has none to read
    // them from, so it states its readiness instead of reporting a failure.
    expect(screen.queryByText(/bot(s)? running/)).toBeNull();
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it('opens a down lane at its own account, where the shell badge opens it too', async () => {
    await renderCard(OFFLINE_LANE);

    // An account whose lane has gone unreachable is still that account, and
    // its workspace explains the outage in place (FR-096). Sending the card
    // to Configuration instead would make one account two different places
    // depending on whether it was opened from the list or from the top bar.
    expect(screen.getByRole('link').getAttribute('href')).toBe(OFFLINE_WORKSPACE_URL);
  });

  it('states a down lane’s lifecycle rather than asking a clerk that cannot answer', async () => {
    const { readDeskState } = await renderCard(OFFLINE_LANE);

    expect(await screen.findByText('This lane is Unreachable.')).toBeTruthy();
    // The directory already carries this fact. Reading a clerk that is by
    // definition not answering and reporting the refusal would fabricate an
    // outage over a known one — the same reason an undeclared capability is
    // never read (FR-097).
    expect(readDeskState).not.toHaveBeenCalled();
    expect(screen.queryByText('Readiness unavailable')).toBeNull();
    expect(screen.queryByText(/bot(s)? running/)).toBeNull();
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it('reads nothing from a lane it cannot show an account for', async () => {
    const { getAccount, getCatalog } = await renderCard(OFFLINE_LANE);

    expect(getAccount).not.toHaveBeenCalled();
    expect(getCatalog).not.toHaveBeenCalled();
  });

  it('reads its facts under the binding generation and routing epoch it rendered', async () => {
    const { getAccount } = await renderCard();

    await vi.waitFor(() => expect(getAccount).toHaveBeenCalled());
    expect(getAccount.mock.calls[0][0]).toMatchObject({
      broker: 'alpaca',
      clerkId: TEST_CLERK_ID,
      accountId: TEST_ACCOUNT_ID,
      bindingGeneration: 3,
      routingEpoch: 4,
    });
  });

  it('keeps a failed equity read to itself — the bot count still lands', async () => {
    await renderCard(testLane(), {
      getAccount: () => Promise.reject(new Error('account read failed')),
    });

    expect(await screen.findByText('Equity unavailable')).toBeTruthy();
    expect(await screen.findByText('1 bot running')).toBeTruthy();
  });

  it('keeps a failed roster read to itself — the equity still lands', async () => {
    await renderCard(testLane(), {
      getCatalog: () => Promise.reject(new Error('roster read failed')),
    });

    expect(await screen.findByText('Bot count unavailable')).toBeTruthy();
    expect(await screen.findByText('$12,345.67')).toBeTruthy();
  });

  it('says a failed readiness read failed, rather than inventing a readiness', async () => {
    // A lane that is up but unbound is the one the headline is read from —
    // its clerk is answering, so a failure here is a genuine failed read.
    await renderCard(UNBOUND_LANE, {
      readDeskState: () => Promise.reject(new Error('desk state unavailable')),
    });

    expect(await screen.findByText('Readiness unavailable')).toBeTruthy();
  });

  it('asks a lane for nothing it has not declared it can serve', async () => {
    const { getCatalog } = await renderCard(
      testLane({ capabilities: ['account_read', 'configuration_manage'] }),
    );

    expect(await screen.findByText('No bot roster on this lane')).toBeTruthy();
    expect(getCatalog).not.toHaveBeenCalled();
  });

  it('carries a deploy intent into the account the operator chooses, landing on its Deploy tab', async () => {
    // The hand-off from strategy validation lands on the list as
    // `?deploy=&strategy=…`; the card is the account-selection step, so the
    // intent has to survive the click by landing straight on that account's
    // Deploy tab rather than merging a now-meaningless `?deploy` forward.
    await renderCard(testLane(), {}, true);

    expect(screen.getByRole('link').getAttribute('href')).toBe(`${WORKSPACE_URL}/deploy`);
  });

  it("shows a lane's account nickname, and its own label when another shares it", async () => {
    const paper = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    const live = testLane({
      clerk_id: 'clrk_live',
      display_label: 'Live',
      provider_summary: { account_nickname: '  strategy lab  ' },
    });
    await renderCard(paper, {}, false, [live]);

    expect(screen.getByText('Strategy lab')).toBeTruthy();
    // Two accounts sharing a name is supported, not refused (ADR 0064
    // Decision 5) — and the accessible name of the card's one link carries
    // the disambiguator with it, because it is named from its own content.
    expect(screen.getByText('(Paper)')).toBeTruthy();
    expect(screen.getByRole('link').textContent).toContain('(Paper)');
  });
});
