/** #2202 follow-up (flagged during independent review): the same
 * unguarded-resource-value defect the issue fixed for `histChart`/
 * `journalPage` also existed in `DualPaneChartComponent.indicatorCategories`
 * (`dual-pane-chart.component.ts`), one layer further down — a `.ts`
 * `computed()`, not a template binding, so it fell outside
 * `resource-value-guard.contract.spec.ts`'s template-only scan.
 *
 * Before the fix, `indicatorCategories` read
 * `this.supportedIndicatorResource.value()?.names` unguarded. That throws
 * `ResourceValueError` while the indicator-catalog fetch is in its error
 * state, and `indicatorCategories()` is bound in the template only inside
 * `@if (fullscreen())` — so clicking Expand while the catalog call has
 * failed aborted this whole component's render pass, exactly like the
 * Trader-lens history freeze this issue fixed elsewhere.
 *
 * Confirmed by hand: reverting the `hasValue()` guard on
 * `supportedIndicatorResource.value()` makes the case below fail with an
 * uncaught `ResourceValueError` thrown from `indicatorCategories`, instead of
 * the Expand button's label updating.
 */
import { TestBed } from '@angular/core/testing';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { signal } from '@angular/core';
import { throwError } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DUAL_PANE_CHART_FACTORY, DualPaneChartComponent } from './dual-pane-chart.component';
import { IndicatorCatalogService } from '../../../../shared/indicator-catalog/indicator-catalog.service';
import { BotChartIndicatorService } from './bot-chart-indicator.service';

const chartMocks = vi.hoisted(() => ({ createChart: vi.fn() }));

vi.mock('lightweight-charts', () => {
  const createSeriesMarkers = vi.fn().mockReturnValue({ setMarkers: vi.fn() });
  return {
    createChart: chartMocks.createChart,
    createSeriesMarkers,
    AreaSeries: 'AreaSeries',
    CandlestickSeries: 'CandlestickSeries',
    HistogramSeries: 'HistogramSeries',
    LineSeries: 'LineSeries',
    LineType: { Simple: 0, WithSteps: 1 },
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

function createMockChart(): object {
  const mockTimeScale = { fitContent: vi.fn() };
  const createMockSeries = () => ({
    setData: vi.fn(),
    update: vi.fn(),
    applyOptions: vi.fn(),
    createPriceLine: vi.fn(),
  });
  return {
    addSeries: vi.fn().mockImplementation(() => createMockSeries()),
    removeSeries: vi.fn(),
    timeScale: vi.fn().mockReturnValue(mockTimeScale),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  };
}

beforeEach(() => {
  chartMocks.createChart.mockReset().mockImplementation(() => createMockChart());
  TestBed.configureTestingModule({
    providers: [
      { provide: DUAL_PANE_CHART_FACTORY, useValue: chartMocks.createChart },
      {
        provide: IndicatorCatalogService,
        useValue: {
          load: vi.fn().mockResolvedValue(undefined),
          categories: signal([{
            name: 'trend',
            indicators: [{
              name: 'ema',
              category: 'trend',
              description: 'Exponential moving average',
              configurable_params: [],
            }],
          }]),
          loading: signal(false),
        },
      },
      {
        provide: BotChartIndicatorService,
        useValue: {
          calculate: vi.fn(),
          // The defect under test: the indicator-catalog fetch fails.
          supportedIndicators: vi.fn().mockReturnValue(
            throwError(() => new Error('catalog 500')),
          ),
        },
      },
    ],
  });
});

describe('DualPaneChartComponent — indicator-catalog failure isolation (#2202 follow-up)', () => {
  it('expanding the chart while the indicator catalog is errored still updates Expand and renders no crash', async () => {
    const user = userEvent.setup();
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    await user.click(screen.getByRole('button', { name: 'Expand market chart' }));
    await fixture.whenStable();
    fixture.detectChanges();

    // The click's own state update reached the DOM — the errored catalog
    // resource did not abort this render pass.
    expect(screen.getByRole('button', { name: 'Exit expanded market chart' })).toBeTruthy();
    // The indicator rail renders with an empty catalog rather than crashing;
    // no EMA entry is offered since the (failed) supported-set is empty.
    expect(screen.getByRole('complementary', { name: 'Indicator picker rail' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /trend/i })).toBeNull();
  });
});
