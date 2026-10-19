import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { dashboard } from '../testing/dashboard-fixture';
import { DAY_ROWS, figures } from '../testing/run-fixtures';
import { RunDailyChartComponent } from './run-daily-chart.component';

describe('the shared run timeline', () => {
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
});
