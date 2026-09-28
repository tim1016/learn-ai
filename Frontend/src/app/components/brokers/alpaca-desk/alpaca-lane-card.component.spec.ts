import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import {
  provideFleetDirectory,
  testLane,
  TEST_ACCOUNT_ID,
  TEST_CLERK_ID,
} from '../../../fleet/fleet-directory-testing';
import type { LaneDescriptor } from '../../../fleet/fleet-directory.types';
import type { ResourceTarget } from '../../../fleet/resource-target';
import { AlpacaLiveVerdictService } from '../../../services/alpaca-live-verdict.service';
import { fakeAccountMoney, unavailableAccountMoney } from '../../../testing/account-money-fixtures';
import { fakeVerdictState } from '../../../testing/alpaca-live-verdict-fixtures';
import { BrokerV2PanelService, type AccountMoneyView } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { AlpacaLaneCardComponent } from './alpaca-lane-card.component';
import { BrokerConfigurationService } from './configuration/broker-configuration.service';

const WORKSPACE_URL = `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}`;
const SETTINGS_URL = `/brokers/alpaca/clerks/${TEST_CLERK_ID}/settings`;
const OFFLINE_ACCOUNT_ID = 'live-account';
const OFFLINE_WORKSPACE_URL = `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${OFFLINE_ACCOUNT_ID}`;

/** A serving lane whose clerk reported its counts on its last heartbeat. */
function countedLane(overrides: Partial<NonNullable<LaneDescriptor['provider_summary']>> = {}): LaneDescriptor {
  return testLane({
    provider_summary: {
      ...testLane().provider_summary,
      running_count: 2,
      dry_run_count: 1,
      attention_count: 1,
      ...overrides,
    },
  });
}

interface CardDoubles {
  getAccountMoney?: (target: ResourceTarget) => Promise<AccountMoneyView>;
  readDeskState?: (clerkId: string) => Promise<{ headline: string }>;
}

async function renderCard(
  lane: LaneDescriptor = countedLane(),
  doubles: CardDoubles = {},
  deployIntent = false,
  siblings: LaneDescriptor[] = [],
) {
  const getAccountMoney = vi.fn(doubles.getAccountMoney ?? (() => Promise.resolve(fakeAccountMoney())));
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
      { provide: BrokerV2PanelService, useValue: { getAccountMoney } },
      { provide: BrokerConfigurationService, useValue: { readDeskState } },
    ],
  });
  return { view, getAccountMoney, readDeskState };
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
  it('shows the account, its mode worded once, its money, its mini bar and its counts', async () => {
    const { view } = await renderCard();

    expect(screen.getByText('Paper')).toBeTruthy();
    expect(screen.getByText('PAPER · practice money')).toBeTruthy();
    const figures = await screen.findByText(/Account money/);
    expect(figures.textContent?.replace(/\s+/g, ' ').trim()).toBe('Account money $100,000.00');
    expect(screen.getByText(/Free to deploy/).textContent?.replace(/\s+/g, ' ').trim()).toBe('Free to deploy $98,329.57');
    // The mini bar is the account-money read's own segments, named for
    // assistive technology even though the card shows no visible legend.
    const legend = screen.getByRole('list', { name: 'Where Paper’s money is' });
    expect(legend.textContent).toContain('free to deploy');
    expect(view.container.querySelectorAll('.money-bar__track > .money-bar__slice')).toHaveLength(4);
    for (const count of ['2 running', '1 stopped, still holding', '1 Dry Run', '1 needs you']) {
      expect(screen.getByText(count)).toBeTruthy();
    }
  });

  it('frames the card in its lane colour beside the worded mode', async () => {
    await renderCard();

    expect(screen.getByRole('link').getAttribute('data-lane')).toBe('paper');
  });

  it('reads every count from one backend field and adds nothing up', async () => {
    await renderCard(countedLane({ running_count: 0, dry_run_count: 3, attention_count: 0 }), {
      getAccountMoney: () => Promise.resolve(fakeAccountMoney({ account_id: TEST_ACCOUNT_ID, stopped_holding_count: 0 })),
    });

    expect(await screen.findByText('0 running')).toBeTruthy();
    await screen.findByText(/Free to deploy/);
    expect(screen.queryByText(/stopped, still holding/)).toBeNull();
    expect(screen.getByText('3 Dry Run')).toBeTruthy();
    expect(screen.getByText('All clear')).toBeTruthy();
  });

  it('takes the stopped-still-holding count from the money read, never from counting its slices', async () => {
    // The bar carries one stopped slice; the backend's count is what is said.
    await renderCard(countedLane(), {
      getAccountMoney: () => Promise.resolve(fakeAccountMoney({ account_id: TEST_ACCOUNT_ID, stopped_holding_count: 3 })),
    });

    expect(await screen.findByText('3 stopped, still holding')).toBeTruthy();
  });

  it('says the stopped-still-holding count is unknown while the money cannot be read', async () => {
    await renderCard(countedLane(), { getAccountMoney: () => Promise.resolve(unavailableAccountMoney('Wait for a fresh reading.')) });

    expect(await screen.findByText('Stopped holdings unknown')).toBeTruthy();
    expect(screen.queryByText(/stopped, still holding/)).toBeNull();
  });

  it('states an overdrawn account’s shortfall as the backend authored it', async () => {
    await renderCard(countedLane(), {
      getAccountMoney: () => Promise.resolve(fakeAccountMoney({ account_id: TEST_ACCOUNT_ID, account_shortfall_usd: '50.00' })),
    });

    const shortfall = await screen.findByText(/Claims exceed the account by/);
    expect(shortfall.textContent?.replace(/\s+/g, ' ').trim()).toBe('Claims exceed the account by $50.00');
  });

  it('states no shortfall for an account that is not overdrawn', async () => {
    await renderCard();

    await screen.findByText(/Free to deploy/);
    expect(screen.queryByText(/Claims exceed the account/)).toBeNull();
  });

  it('says an attention count the directory omitted is unknown, never zero', async () => {
    const lane = countedLane();
    const { attention_count: _omitted, ...summary } = lane.provider_summary ?? {};
    await renderCard({ ...lane, provider_summary: summary });

    expect(screen.getByText('Attention unknown')).toBeTruthy();
    expect(screen.queryByText('All clear')).toBeNull();
  });

  it('says a count the lane did not report is unknown, never zero', async () => {
    await renderCard(countedLane({ running_count: null, dry_run_count: null, attention_count: null }));

    expect(screen.getByText('Bot counts unavailable')).toBeTruthy();
    expect(screen.getByText('Attention unknown')).toBeTruthy();
    expect(screen.queryByText(/^0 /)).toBeNull();
  });

  it('shows why an account has no money figures in the backend’s own words, never $0', async () => {
    await renderCard(countedLane(), {
      getAccountMoney: () =>
        Promise.resolve(unavailableAccountMoney('No daily loss limit is set for this account, so new entries are refused. Set one in Settings.')),
    });

    expect(await screen.findByText(/No daily loss limit is set for this account/)).toBeTruthy();
    expect(screen.queryByText(/\$0\.00/)).toBeNull();
    expect(screen.queryByRole('list', { name: /money is/ })).toBeNull();
  });

  it('keeps a failed money read to itself and names the next step', async () => {
    await renderCard(countedLane(), { getAccountMoney: () => Promise.reject(new Error('money read failed')) });

    const reason = await screen.findByText(/Account money could not be read\./);
    expect(reason.textContent).toContain('It is read again automatically');
    // The counts still land: they are the directory's, not the failed read's.
    expect(screen.getByText('2 running')).toBeTruthy();
  });

  it('says a refused money read in the backend’s words, with its next step', async () => {
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
    await renderCard(countedLane(), { getAccountMoney: () => Promise.reject(refusal) });

    const reason = await screen.findByText(/cannot be read right now/);
    expect(reason.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'This account’s money cannot be read right now. The account’s records are still being opened. '
        + 'Open the account’s Settings to see why, then retry.',
    );
  });

  it('keeps its figures through a directory refresh instead of re-reading and blanking', async () => {
    const lane = countedLane();
    const { view, getAccountMoney } = await renderCard(lane);
    await screen.findByText(/Free to deploy/);

    // A directory refresh hands the card an identical lane as a new object.
    await view.rerender({ inputs: { lane: structuredClone(lane) }, partialUpdate: true });
    view.fixture.detectChanges();

    expect(screen.getByText(/Free to deploy/)).toBeTruthy();
    expect(screen.queryByText('Reading account money…')).toBeNull();
    expect(getAccountMoney).toHaveBeenCalledTimes(1);
  });

  it('re-reads its money every 30 seconds without blanking the figures', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    try {
      const { getAccountMoney } = await renderCard();
      await vi.waitFor(() => expect(getAccountMoney).toHaveBeenCalledTimes(1));
      await screen.findByText(/Free to deploy/);

      vi.advanceTimersByTime(30_000);

      await vi.waitFor(() => expect(getAccountMoney).toHaveBeenCalledTimes(2));
      expect(screen.getByText(/Free to deploy/)).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
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

  it('opens a lane with no confirmed account on Settings, with the readiness line the server authored', async () => {
    await renderCard(UNBOUND_LANE);

    expect(
      await screen.findByText('Select an Alpaca account to activate this lane'),
    ).toBeTruthy();
    expect(screen.getByRole('link').getAttribute('href')).toBe(SETTINGS_URL);
    // Money and a bot count belong to an account; this lane has none to read
    // them from, so it states its readiness instead of reporting a failure.
    expect(screen.queryByText(/running/)).toBeNull();
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it('opens a down lane at its own account, where the shell badge opens it too', async () => {
    await renderCard(OFFLINE_LANE);

    // An account whose lane has gone unreachable is still that account, and
    // its workspace explains the outage in place (FR-096). Sending the card
    // to Settings instead would make one account two different places
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
    expect(screen.queryByText(/running/)).toBeNull();
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it('reads nothing from a lane it cannot show an account for', async () => {
    const { getAccountMoney } = await renderCard(OFFLINE_LANE);

    expect(getAccountMoney).not.toHaveBeenCalled();
  });

  it('reads its money under the binding generation and routing epoch it rendered', async () => {
    const { getAccountMoney } = await renderCard();

    await vi.waitFor(() => expect(getAccountMoney).toHaveBeenCalled());
    expect(getAccountMoney.mock.calls[0][0]).toMatchObject({
      broker: 'alpaca',
      clerkId: TEST_CLERK_ID,
      accountId: TEST_ACCOUNT_ID,
      bindingGeneration: 3,
      routingEpoch: 4,
    });
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
    const { getAccountMoney } = await renderCard(
      testLane({ capabilities: ['account_read', 'configuration_manage'] }),
    );

    expect(await screen.findByText('This account does not report its money.')).toBeTruthy();
    expect(getAccountMoney).not.toHaveBeenCalled();
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
    expect(screen.getByRole('link', { name: 'Strategy lab (Paper) PAPER · practice money' })).toBeTruthy();
  });

  it('names its link by the account and its mode, and describes it by the rest', async () => {
    await renderCard();
    await screen.findByText(/Free to deploy/);

    // The name is what a screen reader announces for the choice; the hidden
    // money-bar legend and the counts belong in the description, not the name.
    const link = screen.getByRole('link', { name: 'Paper PAPER · practice money' });
    const description = document.getElementById(link.getAttribute('aria-describedby') ?? '');
    expect(description?.textContent).toContain('Free to deploy');
    expect(description?.textContent).toContain('2 running');
  });
});
