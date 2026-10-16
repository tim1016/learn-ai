/** #3098: the bot page's Alpaca comparison card. The page shows it for a bot that trades options and
 * owns the one command on it, the owner's review of a held bot's notice: fenced to the lane the owner
 * was shown, and sent only after a confirmation. Kept apart from `bot-panel-shell.component.spec.ts`
 * so that file's shard stays in budget. */
import { HttpErrorResponse } from '@angular/common/http';
import { type ComponentFixture } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { MessageService } from 'primeng/api';
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { provideFleetDirectory, testLane } from '../../../../fleet/fleet-directory-testing';
import { BrokersService } from '../../../../services/brokers.service';
import { fakeBotPanelView, fakeChartFeed, panelRefusalBody } from '../../../../testing/bot-panel-fixtures';
import { fakeStrategyChartFactory } from '../../../../testing/strategy-chart-fake';
import { fakeStrategyViewDataPlane } from '../../../../testing/strategy-view-data-plane-fakes';
import { fakeStrategyView } from '../../../../testing/strategy-view-fixtures';
import {
  BrokerV2PanelService,
  type OptionBuyingPowerChecks,
  type OptionBuyingPowerHoldReview,
} from '../lib/broker-v2-panel.service';
import type { BotPanelView } from '../lib/broker-v2-panel.types';
import { STRATEGY_CHART_FACTORY } from '../strategy-view/strategy-chart.component';
import { BotPanelShellComponent } from './bot-panel-shell.component';

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: vi.fn().mockReturnValue({ setMarkers: vi.fn() }),
  CandlestickSeries: 'CandlestickSeries',
  LineSeries: 'LineSeries',
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

const charts = fakeStrategyChartFactory(vi);

class StubEventSource {
  addEventListener = vi.fn();
  close = vi.fn();
}

const originalEventSource = globalThis.EventSource;
(globalThis as { EventSource?: unknown }).EventSource = StubEventSource;

const amount = (cents: number) => ({ cents, dollars: (cents / 100).toFixed(2) });

const HOLD = {
  headline: 'Alpaca took more than this bot set aside for an entry',
  explanation: 'Across entry order-1 Alpaca’s option buying power fell $250.00. The bot had set aside $194.06.',
  impact: 'This bot will open nothing new until you have reviewed this.',
  next_step: 'Look at this account’s activity at Alpaca around the entry, then review this notice.',
  review_consequence: 'Once you have reviewed it, this bot may open new option entries again.',
  raised_at_ms: 1_790_000_080_000,
  entries: [{ order_ref: 'order-1', fall: amount(25_000), set_aside: amount(19_406) }],
};

const HELD: OptionBuyingPowerChecks = {
  account_id: 'DUM284968',
  strategy_instance_id: 'sid-001',
  simulation: false,
  judged: true,
  hold: HOLD,
  counts: {
    entries: 1, compared: 1, not_compared: 0, not_compared_by_reason: [],
    sentence: '1 of 1 option entry was compared with Alpaca’s option buying power, 0 not.',
  },
  checks: [],
};

const REVIEWED: OptionBuyingPowerHoldReview = { reviewed: true, state: { ...HELD, hold: null } };

function panel(traded: BotPanelView['traded']): BotPanelView {
  return fakeBotPanelView({ strategy_instance_id: 'sid-001', account_id: 'DUM284968', traded, journal_tail_seq: 5 });
}

function fakes(traded: BotPanelView['traded'], review: ReturnType<typeof vi.fn>) {
  const view = panel(traded);
  return {
    getLiveSnapshot: vi.fn().mockResolvedValue({
      stream_epoch: 'test-epoch',
      surface_version: 1,
      panel: view,
      live_chart: {
        strategy_instance_id: 'sid-001', symbol: 'SPY', trading_date_open_ms: 1_759_239_000_000,
        trading_date_close_ms: 1_759_262_400_000, resolution: '5s', bars: [], fill_markers: [],
        overlay_notices: [], feed: fakeChartFeed(), as_of_ms: 1_759_239_000_000,
      },
    }),
    liveStreamUrl: vi.fn().mockReturnValue('/api/test/live-stream'),
    getCurrentRun: vi.fn().mockRejectedValue(new Error('No current run fixture.')),
    getBudget: vi.fn().mockRejectedValue(new Error('No budget fixture.')),
    getHistoryChart: vi.fn().mockRejectedValue(new Error('No history fixture.')),
    getStrategyView: vi.fn(() => Promise.resolve(fakeStrategyView())),
    getOptionBuyingPowerChecks: vi.fn().mockResolvedValue(HELD),
    reviewOptionBuyingPowerHold: review,
  };
}

async function settle(fixture: ComponentFixture<BotPanelShellComponent>): Promise<void> {
  for (let step = 0; step < 3; step += 1) {
    await fixture.whenStable();
    fixture.detectChanges();
  }
}

async function renderPage(
  traded: BotPanelView['traded'],
  review: ReturnType<typeof vi.fn> = vi.fn().mockResolvedValue(REVIEWED),
  directory = provideFleetDirectory({ observed_at_ms: 1_757_000_000_000, clerks: [testLane({ clerk_id: 'clrk_spec' })] }),
) {
  const service = fakes(traded, review);
  const { fixture } = await render(BotPanelShellComponent, {
    inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
    providers: [
      provideRouter([]),
      directory,
      { provide: STRATEGY_CHART_FACTORY, useValue: charts.create },
      ...fakeStrategyViewDataPlane(vi).providers,
      { provide: BrokerV2PanelService, useValue: service },
      { provide: BrokersService, useValue: { checkSqliteRecoveryAction: vi.fn() } },
      { provide: MessageService, useValue: { add: vi.fn() } },
    ],
  });
  await settle(fixture);
  return { fixture, service, directory };
}

const OPTIONS = { kind: 'option', symbol: 'SPY' } as const;

describe('BotPanelShellComponent — Alpaca option buying power (#3098)', () => {
  beforeEach(() => {
    charts.created.length = 0;
    localStorage.clear();
  });

  afterAll(() => {
    globalThis.EventSource = originalEventSource;
  });

  it('shows the comparison card for a bot that trades options, read through its lane', async () => {
    const { service } = await renderPage(OPTIONS);

    expect(await screen.findByRole('region', { name: 'Alpaca option buying power' })).toBeTruthy();
    expect(service.getOptionBuyingPowerChecks).toHaveBeenCalledWith(
      expect.objectContaining({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001' }),
      'sid-001',
    );
  });

  it('shows no card, and reads nothing, for a bot that trades shares', async () => {
    const { service } = await renderPage({ kind: 'stock', symbol: 'SPY' });

    expect(screen.queryByRole('region', { name: 'Alpaca option buying power' })).toBeNull();
    expect(service.getOptionBuyingPowerChecks).not.toHaveBeenCalled();
  });

  it('sends the review once confirmed, under the lane the owner was shown, and re-reads the panel', async () => {
    const user = userEvent.setup();
    const review = vi.fn().mockResolvedValue(REVIEWED);
    const { fixture, service } = await renderPage(OPTIONS, review);
    const readsBefore = service.getLiveSnapshot.mock.calls.length;

    await user.click(await screen.findByRole('button', { name: 'Review this notice…' }));
    expect(review).not.toHaveBeenCalled();
    await user.click(within(await screen.findByTestId('typed-halt-confirm-dialog')).getByRole('button', { name: 'Mark as reviewed' }));
    await settle(fixture);

    expect(review).toHaveBeenCalledTimes(1);
    expect(review).toHaveBeenCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
        bindingGeneration: 3, routingEpoch: 4, idempotencyKey: expect.any(String),
      }),
      'sid-001',
    );
    expect(service.getLiveSnapshot.mock.calls.length).toBeGreaterThan(readsBefore);
    expect(screen.queryByRole('button', { name: 'Review this notice…' })).toBeNull();
  });

  it('refuses the review, and sends nothing, when the lane was rebound while the card was open', async () => {
    const user = userEvent.setup();
    const review = vi.fn().mockResolvedValue(REVIEWED);
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000, clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const { fixture } = await renderPage(OPTIONS, review, directory);
    await screen.findByRole('button', { name: 'Review this notice…' });

    directory.rebind({
      observed_at_ms: 1_757_000_000_001,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 4 })],
    });
    await directory.useValue.refresh?.();
    await settle(fixture);
    await user.click(screen.getByRole('button', { name: 'Review this notice…' }));
    await user.click(within(await screen.findByTestId('typed-halt-confirm-dialog')).getByRole('button', { name: 'Mark as reviewed' }));

    expect(review).not.toHaveBeenCalled();
    expect(await screen.findByText(/rebound while the action was open/i)).toBeTruthy();
  });

  it('keeps the hold and shows the backend’s refusal when the review is refused', async () => {
    const user = userEvent.setup();
    const refused = new HttpErrorResponse({
      status: 409,
      error: panelRefusalBody({
        message: 'The Clerk is not serving this account right now.',
        why: 'A review is recorded only by a serving Clerk.',
        next_action: 'Try again once the account is back.',
        reason_code: 'CLERK_UNAVAILABLE',
      }),
    });
    const { fixture } = await renderPage(OPTIONS, vi.fn().mockRejectedValue(refused));

    await user.click(await screen.findByRole('button', { name: 'Review this notice…' }));
    await user.click(within(await screen.findByTestId('typed-halt-confirm-dialog')).getByRole('button', { name: 'Mark as reviewed' }));
    await settle(fixture);

    expect(await screen.findByText('The Clerk is not serving this account right now.')).toBeTruthy();
    expect(screen.getByText(HOLD.headline)).toBeTruthy();
  });
});
