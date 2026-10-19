import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { RunDetailService } from '../run-detail/run-detail.service';
import { TIMELINE_CHART_FACTORY, TIMELINE_COLOURS } from '../run-detail/session/timeline-chart.component';
import { sessionDetail, tradesPage, TUESDAY_OPEN } from '../run-detail/testing/detail-fixtures';
import { fakeRunDetail } from '../run-detail/testing/fake-run-detail';
import { fakeTimelineChart } from '../run-detail/testing/timeline-chart-fake';
import { reads } from '../testing/page-actions';
import { runDetail } from '../testing/run-fixtures';
import { RunResultComponent } from './run-result.component';

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: vi.fn(),
  LineSeries: 'LineSeries',
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

async function renderResult() {
  const detail = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()), session: vi.fn(async () => sessionDetail()) });
  const chart = fakeTimelineChart(vi);
  const view = await render(RunResultComponent, {
    inputs: { run: runDetail() },
    providers: [
      { provide: RunDetailService, useValue: detail },
      { provide: TIMELINE_CHART_FACTORY, useValue: chart.factory },
      { provide: TIMELINE_COLOURS, useValue: (_host: HTMLElement, cssVar: string) => `colour(${cssVar})` },
    ],
  });
  fireEvent.click(screen.getByText('Leg trades & session records'));
  fireEvent.click(screen.getByText('Coverage & what did not trade'));
  fireEvent.click(screen.getByText('Daily ledger'));
  await screen.findByRole('table', { name: 'Leg trades' });
  return { view, detail, chart };
}

const dayRegion = () => screen.getByRole('region', { name: 'One day' });

describe('the result of a run, looking closely', () => {
  it('puts the visual monthly summary above the day audit, then trades and one day', async () => {
    const { view } = await renderResult();

    const order = ['app-options-backtest-run-month-table', 'app-options-backtest-run-day-table', 'app-options-backtest-run-trades', 'app-options-backtest-session-view'].map((selector) => view.container.querySelector(selector));
    order.slice(1).forEach((each, at) => expect(order[at]?.compareDocumentPosition(each as Node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING));
  });

  it('offers each day of the table to look at, named by the day', async () => {
    await renderResult();

    const table = screen.getByRole('region', { name: 'By day' });
    expect(within(table).getAllByRole('button', { name: /^Look at / }).map((button) => button.getAttribute('aria-label'))).toEqual([
      'Look at 2026-09-14', 'Look at 2026-09-15', 'Look at 2026-09-16', 'Look at 2026-09-17', 'Look at 2026-09-18',
    ]);
  });

  it('opens the day of a row of the table: reads that day for this run and moves the keyboard into it', async () => {
    const { detail } = await renderResult();

    fireEvent.click(screen.getByRole('button', { name: 'Look at 2026-09-15' }));

    await within(dayRegion()).findByText('Traded');
    expect(detail.session).toHaveBeenCalledWith(7, TUESDAY_OPEN);
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'One day' }));
    expect(reads(within(dayRegion()).getByRole('combobox', { name: 'Day' }))).toContain('2026-09-15 · Traded');
  });

  it('opens the day of a trade: the same day, from the trade’s own control', async () => {
    const { detail } = await renderResult();

    fireEvent.click(screen.getByRole('button', { name: 'Look at 2026-09-16, leg put, first entry' }));

    await within(dayRegion()).findByText('Traded');
    expect(detail.session).toHaveBeenCalledWith(7, TUESDAY_OPEN + 24 * 60 * 60_000);
  });

  it('gives the keyboard back to the control that asked, whichever it was, when the day is closed', async () => {
    await renderResult();
    const fromTable = screen.getByRole('button', { name: 'Look at 2026-09-15' });
    fireEvent.click(fromTable);
    await within(dayRegion()).findByText('Traded');

    fireEvent.click(within(dayRegion()).getByRole('button', { name: 'Close this day' }));
    await waitFor(() => expect(document.activeElement).toBe(fromTable));

    const fromTrade = screen.getByRole('button', { name: 'Look at 2026-09-15, leg call, re-entry 1' });
    fireEvent.click(fromTrade);
    await within(dayRegion()).findByText('Traded');
    fireEvent.click(within(dayRegion()).getByRole('button', { name: 'Close this day' }));

    await waitFor(() => expect(document.activeElement).toBe(fromTrade));
  });

  it('asks for the same day again after it was closed, and shows it again', async () => {
    const { detail } = await renderResult();
    const button = screen.getByRole('button', { name: 'Look at 2026-09-15' });
    fireEvent.click(button);
    await within(dayRegion()).findByText('Traded');
    fireEvent.click(within(dayRegion()).getByRole('button', { name: 'Close this day' }));
    await waitFor(() => expect(within(dayRegion()).queryByText('Traded')).toBeNull());

    fireEvent.click(button);

    await within(dayRegion()).findByText('Traded');
    expect(detail.session).toHaveBeenCalledTimes(2);
  });

  it('shows the day asked for last when two are asked for one after the other', async () => {
    const { detail } = await renderResult();

    fireEvent.click(screen.getByRole('button', { name: 'Look at 2026-09-15' }));
    await within(dayRegion()).findByText('Traded');
    fireEvent.click(screen.getByRole('button', { name: 'Look at 2026-09-18' }));

    await waitFor(() => expect(detail.session).toHaveBeenCalledTimes(2));
    expect(detail.session.mock.lastCall?.[1]).toBe(TUESDAY_OPEN + 3 * 24 * 60 * 60_000);
  });
});
