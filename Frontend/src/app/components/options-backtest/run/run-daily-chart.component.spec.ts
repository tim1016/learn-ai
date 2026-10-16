import { fireEvent, render, screen } from '@testing-library/angular';
import { expect, it } from 'vitest';

import { DAY_ROWS, figures } from '../testing/run-fixtures';
import { RunDailyChartComponent } from './run-daily-chart.component';

it('shows the authored equity path and opens the selected session from its daily bar', async () => {
  const run = figures();
  const asked: number[] = [];
  const { container } = await render(RunDailyChartComponent, {
    inputs: { rows: DAY_ROWS, equityCurve: run.equity_curve, drawdown: run.drawdown_by_dollars },
    on: { openDay: (request: { openMs: number }) => asked.push(request.openMs) },
  });

  expect(container.querySelector('polyline')?.getAttribute('points')).toBe('0,148 200,131 400,138 600,138 800,138 1000,12');
  expect(container.querySelector('polygon')?.getAttribute('points')).toBe('400,0 800,0 800,160 400,160');

  fireEvent.click(screen.getByRole('button', { name: /Look at 2026-09-15/ }));
  expect(asked).toEqual([DAY_ROWS[1].session_open_ms]);
});
