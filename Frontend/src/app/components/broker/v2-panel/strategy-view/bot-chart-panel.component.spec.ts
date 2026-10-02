import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import { fakeStrategyChartFactory } from '../../../../testing/strategy-chart-fake';
import { fakeStrategyViewDataPlane } from '../../../../testing/strategy-view-data-plane-fakes';
import {
  BEFORE_START_TEXT,
  barCloseMs,
  fakeRecentDecision,
  fakeStrategyView,
} from '../../../../testing/strategy-view-fixtures';
import { RecentDecisionsListComponent } from '../bot-page/recent-decisions-list/recent-decisions-list.component';
import type { StrategyViewResponse } from '../lib/broker-v2-panel.types';
import { BotChartPanelComponent } from './bot-chart-panel.component';
import { STRATEGY_CHART_FACTORY } from './strategy-chart.component';
import type { StrategyViewFailure } from './strategy-view-model';
import { GATE_CANDLE_COLORS } from './strategy-view-model';

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: vi.fn().mockReturnValue({ setMarkers: vi.fn() }),
  CandlestickSeries: 'CandlestickSeries',
  LineSeries: 'LineSeries',
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

const charts = fakeStrategyChartFactory(vi);

function candleColors(): string[] {
  const data: { color: string }[] = charts.current().candles().setData.mock.calls.at(-1)?.[0] ?? [];
  return data.map((candle) => candle.color);
}

const minute = (ms: number) => formatTimestampDisplay(ms, { mode: 'local', granularity: 'minute' });
/** A bar's date and minute, as the candle popover titles it: warmup bars come from earlier days. */
const barTitle = (ms: number) => `${formatTimestampDisplay(ms, { mode: 'local', granularity: 'date' })} ${minute(ms)}`;
let dataPlane = fakeStrategyViewDataPlane(vi);
const providers = () => [{ provide: STRATEGY_CHART_FACTORY, useValue: charts.create }, ...dataPlane.providers];

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotChartPanelComponent],
  template: `
    <app-bot-chart-panel symbol="SPY" [view]="view()" [failure]="failure()" [tape]="tape" (retry)="retries.set(retries() + 1)" />
    <ng-template #tape><p>Tape stand-in</p></ng-template>
    <p>Retries: {{ retries() }}</p>
  `,
})
class PanelHost {
  readonly view = signal<StrategyViewResponse | null>(fakeStrategyView());
  readonly failure = signal<StrategyViewFailure | null>(null);
  readonly retries = signal(0);
}

/** The bot page's wiring: one selection shared by the chart panel and the decisions list. */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotChartPanelComponent, RecentDecisionsListComponent],
  template: `
    <app-bot-chart-panel symbol="SPY" [view]="view" [tape]="tape" [(selectedBarCloseMs)]="selected" />
    <ng-template #tape><p>Tape stand-in</p></ng-template>
    <app-recent-decisions-list [decisions]="decisions" [strategyView]="view" [(selectedBarCloseMs)]="selected" />
  `,
})
class LinkedHost {
  readonly view = fakeStrategyView();
  readonly decisions = [fakeRecentDecision(12, 3), fakeRecentDecision(11, 2)];
  readonly selected = signal<number | null>(null);
}

async function renderPanel(setup: (host: PanelHost) => void = () => undefined) {
  const rendered = await render(PanelHost, { providers: providers() });
  setup(rendered.fixture.componentInstance);
  await rendered.fixture.whenStable();
  return rendered;
}

function decisionRow(barIndex: number): HTMLElement {
  const list = screen.getByRole('list', { name: 'Recent decisions' });
  const row = within(list).getAllByRole('listitem').find((item) => item.textContent?.includes(minute(barCloseMs(barIndex))));
  if (row === undefined) throw new Error(`No decision row for bar ${barIndex}.`);
  return row;
}

describe('BotChartPanelComponent (#2639)', () => {
  beforeEach(() => {
    charts.created.length = 0;
    dataPlane = fakeStrategyViewDataPlane(vi);
    localStorage.clear();
  });

  it('opens on the strategy’s decision candles and creates the tape only when its tab opens', async () => {
    const user = userEvent.setup();
    await renderPanel();

    const strategyTab = screen.getByRole('tab', { name: 'Strategy · 15m' });
    expect(strategyTab.getAttribute('aria-selected')).toBe('true');
    expect(screen.queryByText('Tape stand-in')).toBeNull();
    expect(charts.created).toHaveLength(1);

    await user.click(screen.getByRole('tab', { name: 'Tape' }));
    expect(screen.getByText('Tape stand-in')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^Gate:/ })).toBeNull();

    await user.click(strategyTab);
    // Both kept, out of sight, so each chart's own state survives the trip.
    expect(screen.getByText('Tape stand-in', { ignore: false })).toBeTruthy();
    expect(charts.created).toHaveLength(1);
    expect(screen.getByRole('button', { name: /^Gate:/ })).toBeTruthy();
  });

  it('switches the shading to the gate the viewer picks, the strategy’s rule listed first, and remembers it', async () => {
    const user = userEvent.setup();
    await renderPanel();

    expect(screen.getByRole('button', { name: /^Gate: Bar 3 in 20–80 Strategy/ })).toBeTruthy();
    const options = within(screen.getByRole('dialog', { name: 'Dark Bright Gate' })).getAllByRole('radio');
    expect(options.map((option) => option.closest('label')?.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      'Bar 3 in 20–80 Strategy 20 ≤ BAR3 ≤ 80',
      'Foo over close Mine FOO7 − close > 0',
    ]);
    expect(screen.getByText(/Gates only shade; they never trade\./)).toBeTruthy();

    await user.click(screen.getByRole('radio', { name: /Foo over close/ }));

    expect(screen.getByRole('button', { name: /^Gate: Foo over close Mine/ })).toBeTruthy();
    expect(candleColors()).toEqual([
      GATE_CANDLE_COLORS.upDark,
      GATE_CANDLE_COLORS.downBright,
      GATE_CANDLE_COLORS.upBright,
      GATE_CANDLE_COLORS.downDark,
    ]);
    expect(localStorage.getItem('broker-v2.strategy-view.gate.v1:foo_cross')).toBe('g_mine');
  });

  it('opens on the gate this viewer chose before, or the strategy’s default once that gate is gone', async () => {
    localStorage.setItem('broker-v2.strategy-view.gate.v1:foo_cross', 'g_mine');
    await renderPanel();
    expect(screen.getByRole('button', { name: /^Gate: Foo over close/ })).toBeTruthy();
  });

  it('keeps the viewer’s gate through a re-read of the same strategy, even when this browser stores nothing', async () => {
    const user = userEvent.setup();
    const blocked = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Storage is blocked.', 'SecurityError');
    });
    try {
      const { fixture } = await renderPanel();
      await user.click(screen.getByRole('radio', { name: /Foo over close/ }));

      fixture.componentInstance.view.set(fakeStrategyView({ notices: ['A newer read.'] }));
      await fixture.whenStable();

      expect(screen.getByRole('button', { name: /^Gate: Foo over close Mine/ })).toBeTruthy();
    } finally {
      blocked.mockRestore();
    }
  });

  it('falls back to the strategy’s default gate when the remembered one no longer exists', async () => {
    localStorage.setItem('broker-v2.strategy-view.gate.v1:foo_cross', 'g_deleted');
    await renderPanel();
    expect(screen.getByRole('button', { name: /^Gate: Bar 3 in 20–80/ })).toBeTruthy();
  });

  it('lists the strategy’s own drawn values as always on, under the backend’s labels', async () => {
    await renderPanel();

    const indicators = screen.getByRole('dialog', { name: 'Indicators' });
    expect(within(indicators).getByText('Strategy’s own · bot’s values')).toBeTruthy();
    const items = within(indicators).getAllByRole('listitem').map((item) => item.textContent?.replace(/\s+/g, ' ').trim());
    expect(items).toEqual(['Foo 7 always on', 'Bar 3 · 20–80 band always on']);
    const legend = screen.getByRole('list', { name: 'Chart legend' });
    expect(within(legend).getByText('Foo 7')).toBeTruthy();
    expect(within(legend).getByText('Bar 3')).toBeTruthy();
    expect(within(legend).queryByText('Baz 1')).toBeNull();
  });

  it('shows a clicked candle’s bar, prices, checks and what the active gate made of it', async () => {
    await renderPanel();

    charts.current().click(barCloseMs(2) / 1000);
    await screen.findByRole('dialog', { name: `${barTitle(barCloseMs(2))} bar · No Action` });

    const popover = screen.getByRole('dialog', { name: /bar · No Action/ });
    expect(within(popover).getByText('O 499 · H 503 · L 498 · C 500')).toBeTruthy();
    expect(within(popover).getByText('no zap at bar 2')).toBeTruthy();
    expect(within(popover).getByText(/Gate “Bar 3 in 20–80”:/).textContent).toContain('no result');

    charts.current().click(barCloseMs(1) / 1000);
    const before = await screen.findByRole('dialog', { name: `${barTitle(barCloseMs(1))} bar · ${BEFORE_START_TEXT}` });
    expect(within(before).getByText(/Gate “Bar 3 in 20–80”:/).textContent).toContain('fails, dark candle');
  });

  it('renders the backend’s notices verbatim and a calm empty state before the first decision bar', async () => {
    await renderPanel((host) => host.view.set(fakeStrategyView({
      candles: [],
      notices: ['Bars from before the bot started are unavailable for this run.'],
    })));

    expect(within(screen.getByRole('list', { name: 'Strategy view notices' }))
      .getByText('Bars from before the bot started are unavailable for this run.')).toBeTruthy();
    expect(within(screen.getByRole('tabpanel', { name: 'Strategy · 15m' })).getByRole('status').textContent).toContain(
      'No decision bars yet. The first appears when the bot’s first 15m bar closes.',
    );
    expect(screen.queryByRole('group', { name: /decision candles/ })).toBeNull();
  });

  it('shows a failed read in the backend’s words, with one Retry', async () => {
    const user = userEvent.setup();
    await renderPanel((host) => {
      host.view.set(null);
      host.failure.set({ message: 'This bot’s Clerk is unavailable.', why: 'Restore the selected authority, then refresh.' });
    });

    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('This bot’s Clerk is unavailable.')).toBeTruthy();
    expect(within(alert).getByText('Restore the selected authority, then refresh.')).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Strategy' })).toBeTruthy();

    await user.click(within(alert).getByRole('button', { name: 'Retry strategy view' }));
    expect(screen.getByText('Retries: 1')).toBeTruthy();
  });

  describe('beside the decisions list', () => {
    it('passes AXE with a candle’s popover open and a decision expanded', async () => {
      const user = userEvent.setup();
      await render(LinkedHost, { providers: providers() });

      charts.current().click(barCloseMs(2) / 1000);
      await screen.findByRole('dialog', { name: /bar · No Action/ });
      await user.click(within(decisionRow(3)).getByRole('button'));

      // jsdom lays nothing out, so contrast is checked in the browser, not here.
      const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
      expect(results.violations).toEqual([]);
    });

    it('shows a candle’s checks and its decision row’s checks from one table', async () => {
      const user = userEvent.setup();
      await render(LinkedHost, { providers: providers() });

      charts.current().click(barCloseMs(2) / 1000);
      const popover = await screen.findByRole('dialog', { name: /bar · No Action/ });
      const fromCandle = within(popover).getByRole('table', { name: 'Checks' }).textContent;

      await user.click(within(decisionRow(2)).getByRole('button'));
      const fromRow = within(decisionRow(2)).getByRole('table', { name: 'Checks' }).textContent;

      expect(fromRow).toBe(fromCandle);
      expect(fromRow).toContain('no zap at bar 2');
    });
  });
});
