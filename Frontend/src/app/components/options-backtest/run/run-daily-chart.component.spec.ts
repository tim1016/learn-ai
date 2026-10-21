import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { dashboard } from '../testing/dashboard-fixture';
import { DAY_ROWS, figures } from '../testing/run-fixtures';
import { RunDailyChartComponent } from './run-daily-chart.component';
import type { DayRow } from '../options-backtest.types';
import run76 from '../../../../../../PythonDataService/tests/fixtures/options_evidence/run76.json';

describe('the shared run timeline', () => {
  it('selects the recorded independent dollar and percentage maximum episodes for source76', async () => {
    const f = run76.figures;
    const { container } = await render(RunDailyChartComponent, { inputs: { rows: f.by_day as DayRow[], equityCurve: f.equity_curve,
      drawdown: f.drawdown_by_dollars, percentageDrawdown: f.drawdown_by_fraction } });
    const dollarShade = container.querySelector('.drawdown-span')?.getAttribute('points');
    expect(screen.getByText(/Selected dollars maximum/).textContent).toContain('$1,754.10 · 9.1%');
    expect(screen.getByText(/Selected dollars maximum/).textContent).toContain('2025-04-24 → trough 2025-07-01');
    fireEvent.click(screen.getByRole('button', { name: 'Show largest percentage drawdown' }));
    expect(screen.getByText(/Selected percentage maximum/).textContent).toContain('$1,633.50 · 12.4%');
    expect(screen.getByText(/Selected percentage maximum/).textContent).toContain('2025-02-21 → trough 2025-03-25');
    expect(container.querySelector('.drawdown-span')?.getAttribute('points')).not.toBe(dollarShade);
  });
  it('aligns each bar with its own equity point and highlights the same session in all three plots', async () => {
    const run = figures(), asked: number[] = [];
    const { container } = await render(RunDailyChartComponent, {
      inputs: { rows: DAY_ROWS, equityCurve: run.equity_curve, drawdown: run.drawdown_by_dollars, dailyMetrics: dashboard().daily },
      on: { openDay: (request: { openMs: number }) => asked.push(request.openMs) },
    });
    const circle = container.querySelectorAll('svg.equity circle')[2];
    const bar = screen.getByRole('button', { name: /Look at 2026-09-15/ });
    expect(Number(circle.getAttribute('cx'))).toBeCloseTo(Number(bar.getAttribute('x')) + Number(bar.getAttribute('width')) / 2, 10);
    fireEvent.mouseEnter(bar);
    expect(container.querySelectorAll('.guide')).toHaveLength(3);
    expect(new Set([...container.querySelectorAll('.guide')].map((guide) => guide.getAttribute('x1'))).size).toBe(1);
    expect(container.querySelectorAll('.plot-scroll')).toHaveLength(1);
    expect(screen.getByText('2026-09')).toBeTruthy();
    fireEvent.click(bar);
    expect(asked).toEqual([DAY_ROWS[1].session_open_ms]);
  });

  it('uses exact source dollars in readouts even above the browser integer range', async () => {
    const amount = '9007199254740993.01';
    const row = { ...DAY_ROWS[0], net_profit_usd: amount };
    const { container } = await render(RunDailyChartComponent, { inputs: { rows: [row], equityCurve: [{ session_open_ms: null, equity_usd: amount }, { session_open_ms: row.session_open_ms, equity_usd: amount }] } });
    fireEvent.mouseEnter(screen.getByRole('button', { name: /Look at/ }));
    expect(container.querySelector('.readout')?.textContent).toContain('$9,007,199,254,740,993.01');
    expect(container.querySelector('.equity-scale')?.textContent).toContain('$9,007,199,254,740,993.01');
  });

  it('withholds a misaligned equity path instead of scaling hidden points', async () => {
    const path = figures().equity_curve.map((point, index) => index === 2
      ? { ...point, session_open_ms: 1, equity_usd: '9007199254740993.01' } : point);
    const { container } = await render(RunDailyChartComponent, {
      inputs: { rows: DAY_ROWS, equityCurve: path, dailyMetrics: dashboard().daily },
    });
    expect(screen.getByText(/recorded chart series do not align by session/i)).toBeTruthy();
    expect(container.querySelector('.equity polyline')).toBeNull();
    expect(container.querySelector('.equity-scale')?.textContent).not.toContain('9,007,199,254,740,993');
    expect(container.querySelectorAll('svg.daily .bar')).toHaveLength(DAY_ROWS.length);
    expect(screen.getByText('2026-09')).toBeTruthy();
  });

  it('keeps month ticks when the dashboard projection is absent', async () => {
    await render(RunDailyChartComponent, { inputs: { rows: DAY_ROWS } });
    expect(screen.getByText('2026-09')).toBeTruthy();
  });
  it('leaves a legacy missing peak date unshaded instead of assigning it to starting cash', async () => {
    const recorded = figures().drawdown_by_dollars;
    if (!recorded) throw new Error('Expected a retained drawdown fixture.');
    const fall = { ...recorded };
    delete fall.peak_session_open_ms;
    const { container } = await render(RunDailyChartComponent, { inputs: { rows: DAY_ROWS, equityCurve: figures().equity_curve, drawdown: fall } });
    expect(container.querySelector('.drawdown-span')).toBeNull();
    expect(screen.getByText(/Peak not recorded/)).toBeTruthy();
    expect(screen.getByText(/Chart annotation unavailable/)).toBeTruthy();
  });
});
