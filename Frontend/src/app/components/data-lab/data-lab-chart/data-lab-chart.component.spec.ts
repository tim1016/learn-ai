/**
 * Data-source notice — the chart tells the operator when part of a range came
 * straight from the market-data provider instead of the data lake.
 *
 * The pin that matters: the notice copy comes from a closed map keyed by the
 * backend's machine code, and the code itself is never rendered. A code the UI
 * does not know produces silence, not a leaked identifier.
 */
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import {
  DataLabChartComponent,
  type BarSources,
  type ChartDataResponse,
} from './data-lab-chart.component';

// The DOM chart is not exercised here — only the notice above it.
vi.mock('lightweight-charts', () => {
  const series = () => ({
    setData: vi.fn(),
    update: vi.fn(),
    applyOptions: vi.fn(),
    createPriceLine: vi.fn(),
  });
  const chart = () => ({
    addSeries: vi.fn(series),
    removeSeries: vi.fn(),
    remove: vi.fn(),
    applyOptions: vi.fn(),
    resize: vi.fn(),
    timeScale: vi.fn(() => ({
      applyOptions: vi.fn(),
      fitContent: vi.fn(),
      getVisibleLogicalRange: vi.fn(() => null),
      setVisibleLogicalRange: vi.fn(),
      setVisibleRange: vi.fn(),
      subscribeVisibleLogicalRangeChange: vi.fn(),
    })),
    subscribeCrosshairMove: vi.fn(),
    priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
  });
  return {
    createChart: vi.fn(chart),
    CandlestickSeries: 'CandlestickSeries',
    HistogramSeries: 'HistogramSeries',
    LineSeries: 'LineSeries',
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

const NOTICE_SELECTOR = '.source-notice';

function chartResponse(barSources?: ChartDataResponse['bar_sources']): ChartDataResponse {
  const response: ChartDataResponse = {
    bars: [{ t: 1_764_167_400_000, o: 100, h: 101, l: 99, c: 100.5, v: 1000 }],
    indicators: [],
    quality: {
      raw_bar_count: 1,
      resampled_bar_count: 1,
      duplicates_removed: 0,
      gaps_found: 0,
      largest_gap_minutes: 0,
      missing_sessions: 0,
      session_coverage_pct: 100,
      synthetic_bars: 0,
      gap_details: [],
      missing_session_dates: [],
    },
    allowed_timeframes: ['15m'],
    estimated_bars_per_timeframe: { '15m': 1 },
    recommended_timeframe: '15m',
    meta: { cached_resample: false, cached_indicators: false },
  };
  return barSources ? { ...response, bar_sources: barSources } : response;
}

function cachedSnapshot(barSources?: BarSources | null) {
  const { bars, indicators, quality, allowed_timeframes, estimated_bars_per_timeframe } = chartResponse();
  return {
    bars,
    indicators,
    quality,
    allowedTimeframes: allowed_timeframes,
    estimatedBarsPerTimeframe: estimated_bars_per_timeframe,
    recommendedTimeframe: '15m',
    visibleIndicatorIds: [],
    timeframe: '15m',
    ...(barSources === undefined ? {} : { barSources }),
  };
}

async function renderChart() {
  return render(DataLabChartComponent, {
    inputs: {
      ticker: 'SPY',
      fromDate: '2025-11-26',
      toDate: '2025-12-01',
      session: 'rth',
      forwardFill: false,
      timeframe: '15m',
    },
    providers: [provideHttpClient(), provideHttpClientTesting()],
  });
}

async function renderChartWith(response: ChartDataResponse) {
  const { fixture, container } = await renderChart();
  const http = TestBed.inject(HttpTestingController);
  const emitted: { barSources: BarSources | null }[] = [];
  fixture.componentInstance.dataLoaded.subscribe((event) => emitted.push(event));

  fixture.componentInstance.fetchData();
  http.expectOne((candidate) => candidate.url.endsWith('/api/chart/data')).flush(response);
  await waitFor(() => expect(fixture.componentInstance.quality()).not.toBeNull());
  fixture.detectChanges();

  return { fixture, container: container as HTMLElement, emitted };
}

describe('DataLabChartComponent data-source notice', () => {
  it('shows the provider-fallback notice when the response says history was provider-served', async () => {
    const { container } = await renderChartWith(
      chartResponse({
        boundary_ms_utc: 1_764_599_400_000,
        notice_code: 'history_provider_fallback',
      }),
    );

    const notice = container.querySelector(NOTICE_SELECTOR);
    expect(notice).not.toBeNull();
    expect(notice?.textContent).toContain('not in the data lake yet');
    // The machine code never reaches the screen.
    expect(container.textContent).not.toContain('history_provider_fallback');
  });

  it('explains the raw-only lake when adjusted prices force the provider path', async () => {
    const { container } = await renderChartWith(
      chartResponse({
        boundary_ms_utc: null,
        notice_code: 'adjusted_prices_provider_only',
      }),
    );

    expect(screen.getByRole('status').textContent).toContain('unadjusted prices only');
    expect(container.textContent).not.toContain('adjusted_prices_provider_only');
  });

  it('explains a symbol the lake does not carry', async () => {
    const { container } = await renderChartWith(
      chartResponse({ boundary_ms_utc: null, notice_code: 'symbol_provider_only' }),
    );

    expect(screen.getByRole('status').textContent).toContain('does not carry this symbol');
    expect(container.textContent).not.toContain('symbol_provider_only');
  });

  it('shows no notice when the response carries no source indicator (lake off)', async () => {
    const { container } = await renderChartWith(chartResponse());

    expect(container.querySelector(NOTICE_SELECTOR)).toBeNull();
  });

  it('shows no notice when history is fully lake-backed', async () => {
    const { container } = await renderChartWith(
      chartResponse({ boundary_ms_utc: 1_764_599_400_000, notice_code: null }),
    );

    expect(container.querySelector(NOTICE_SELECTOR)).toBeNull();
  });

  it('stays silent — and leaks nothing — on a code the copy map does not know', async () => {
    const { container } = await renderChartWith(
      chartResponse({ boundary_ms_utc: null, notice_code: 'some_future_code' }),
    );

    expect(container.querySelector(NOTICE_SELECTOR)).toBeNull();
    expect(container.textContent).not.toContain('some_future_code');
  });
});

describe('DataLabChartComponent saved-session provenance', () => {
  it('hands the provenance to the parent so a save can carry it', async () => {
    const { emitted } = await renderChartWith(
      chartResponse({
        boundary_ms_utc: 1_764_599_400_000,
        notice_code: 'history_provider_fallback',
      }),
    );

    expect(emitted).toHaveLength(1);
    expect(emitted[0].barSources).toEqual({
      boundary_ms_utc: 1_764_599_400_000,
      notice_code: 'history_provider_fallback',
    });
  });

  it('emits a null receipt when the response carries none', async () => {
    const { emitted } = await renderChartWith(chartResponse());

    expect(emitted[0].barSources).toBeNull();
  });

  it('restores the notice a saved snapshot was showing', async () => {
    const { fixture, container } = await renderChart();

    fixture.componentInstance.loadCachedData(
      cachedSnapshot({ boundary_ms_utc: null, notice_code: 'history_provider_fallback' }),
    );
    fixture.detectChanges();

    expect(container.querySelector(NOTICE_SELECTOR)?.textContent).toContain('not in the data lake yet');
  });

  it('shows no notice for a snapshot saved before the receipt existed', async () => {
    const { fixture, container } = await renderChart();

    fixture.componentInstance.loadCachedData(cachedSnapshot());
    fixture.detectChanges();

    expect(container.querySelector(NOTICE_SELECTOR)).toBeNull();
  });

  it('does not leave a previous notice standing over restored bars', async () => {
    const { fixture, container } = await renderChartWith(
      chartResponse({ boundary_ms_utc: null, notice_code: 'history_provider_fallback' }),
    );
    expect(container.querySelector(NOTICE_SELECTOR)).not.toBeNull();

    fixture.componentInstance.loadCachedData(cachedSnapshot(null));
    fixture.detectChanges();

    expect(container.querySelector(NOTICE_SELECTOR)).toBeNull();
  });
});
