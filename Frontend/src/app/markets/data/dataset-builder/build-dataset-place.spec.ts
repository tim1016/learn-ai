import { signal } from '@angular/core';
import { screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { environment } from '../../../../environments/environment';
import { RunSessionService } from '../../../services/run-session.service';
import {
  CHART_URL,
  FROM,
  TO,
  chartRequest,
  chartResponse,
  flushCatalog,
  idleRunSession,
  openPlace,
  type OpenedPlace,
} from '../../testing/instrument-place';

// The canvas chart is not exercised here — only that the place reaches Build dataset from it.
vi.mock('lightweight-charts', () => {
  const series = () => ({ setData: vi.fn(), update: vi.fn(), applyOptions: vi.fn(), createPriceLine: vi.fn() });
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

const PLAN_URL = `${environment.pythonServiceUrl}/api/dataset/plan`;
const POLICY_QUERY = `from=${FROM}&to=${TO}&timeframe=5m&session=extended`;
const DOCK_KEY = 'run-dock-expanded:data-lab';

/** The dataset job, standing in: it records what it was asked to run and runs nothing. */
function recordingRun(active = false) {
  const run = {
    ...idleRunSession(),
    state: signal<'idle' | 'fetching' | 'bundling'>(active ? 'fetching' : 'idle'),
    dockState: signal<'idle' | 'active' | 'done' | 'error'>(active ? 'active' : 'idle'),
    headline: signal(active ? 'fetching · chunk 2 of 4' : 'idle — no run in flight'),
    progressPercent: signal<number | null>(active ? 25 : null),
    canCancel: signal(active),
    start: vi.fn(async (_payload: Record<string, unknown>) => undefined),
    cancel: vi.fn(async () => undefined),
  };
  return { run, provider: { provide: RunSessionService, useValue: run } };
}

/** The plan the builder asks Python for by itself, answered with the columns it would list. */
async function answerPlan(opened: OpenedPlace) {
  const request = await waitFor(() => opened.http.expectOne(PLAN_URL));
  request.flush({ session_count: 21, output_columns: ['open', 'high', 'low', 'close', 'volume'] });
  return request;
}

afterEach(() => localStorage.removeItem(DOCK_KEY));

describe('Build dataset, under the instrument', () => {
  it('opens from the header with the committed instrument, window and policy, and starts nothing', async () => {
    const { run, provider } = recordingRun();
    const opened = await openPlace(`/markets/SPY/chart?${POLICY_QUERY}`, [provider]);
    (await chartRequest(opened.http)).flush(chartResponse(500));
    await waitFor(() => expect(opened.store().chartStale()).toBe(false));
    const action = screen.getByRole('link', { name: 'Build dataset' });
    expect(action.getAttribute('href'), 'the action carries the context').toBe(`/markets/SPY/data/build?${POLICY_QUERY}`);

    await userEvent.click(action);

    const plan = await answerPlan(opened);
    expect(plan.request.body).toMatchObject({
      ticker: 'SPY',
      start_ms_utc: Date.UTC(2026, 3, 15),
      end_ms_utc: Date.UTC(2026, 4, 15) + 86_400_000 - 1,
      timespan: 'minute',
      multiplier: 5,
      session: 'extended',
    });
    // The Chart tab's recipe: the product's thirteen default indicators, none re-chosen here.
    expect(plan.request.body['indicator_entries']).toHaveLength(13);
    expect(await screen.findByText('SPY · 5 minute bars · extended session · 13 indicator instances')).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Build dataset' })).toBeTruthy();
    // Opening it asked for a plan and nothing more: no job, no chart.
    expect(run.start).not.toHaveBeenCalled();
    opened.http.expectNone((request) => request.url.includes('/api/jobs') || request.url === CHART_URL);
  });

  it('runs only on its own button, with the payload of what the page shows', async () => {
    const { run, provider } = recordingRun();
    const opened = await openPlace(`/markets/SPY/data/build?${POLICY_QUERY}`, [provider]);
    await answerPlan(opened);
    expect(run.start).not.toHaveBeenCalled();

    await userEvent.click(await screen.findByRole('button', { name: 'Generate dataset ZIP' }));

    await waitFor(() => expect(run.start).toHaveBeenCalledTimes(1));
    expect(run.start.mock.calls[0]?.[0]).toMatchObject({
      ticker: 'SPY',
      start_ms_utc: Date.UTC(2026, 3, 15),
      end_ms_utc: Date.UTC(2026, 4, 15) + 86_400_000 - 1,
      timespan: 'minute',
      multiplier: 5,
      session: 'extended',
    });
  });

  it('leads back to the instrument\'s Data tab with the same context', async () => {
    const opened = await openPlace(`/markets/SPY/data/build?${POLICY_QUERY}`, [recordingRun().provider]);
    await answerPlan(opened);

    const back = await screen.findByRole('link', { name: 'Back to the SPY Data tab' });

    expect(back.getAttribute('href')).toBe(`/markets/SPY/data?${POLICY_QUERY}`);
    expect(screen.getByRole('link', { name: 'Data' }).getAttribute('aria-current'), 'the Data tab stays the open one').toBe('page');
  });

  it('shows a running dataset job in the place\'s dock on every page, cancels it from there, and reads the dock\'s saved choice from the key the Data Lab used', async () => {
    localStorage.setItem(DOCK_KEY, 'true');
    const { run, provider } = recordingRun(true);
    const opened = await openPlace(`/markets/SPY/data/build?${POLICY_QUERY}`, [provider]);
    await answerPlan(opened);

    expect(await screen.findByText('fetching · chunk 2 of 4')).toBeTruthy();
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('25');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(run.cancel).toHaveBeenCalledTimes(1);

    // Another tab of the same place keeps the dock, and the job in it.
    await opened.router.navigateByUrl(`/markets/SPY/returns?${POLICY_QUERY}`);
    opened.harness.fixture.detectChanges();
    expect(screen.getByText('fetching · chunk 2 of 4')).toBeTruthy();
    flushCatalog(opened.http);
  });

  it('offers Build dataset only for an instrument that opens', async () => {
    const opened = await openPlace(`/markets/TOOLONGSYM/chart?${POLICY_QUERY}`, [recordingRun().provider]);

    expect(await screen.findByText(/is not a symbol this place can open/)).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Build dataset' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Saved setups' })).toBeNull();
    opened.http.expectNone(PLAN_URL);
  });
});
