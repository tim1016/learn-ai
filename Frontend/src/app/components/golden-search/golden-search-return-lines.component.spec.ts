import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { etMidnightMs } from '../../shared/date/et-midnight';
import { GoldenSearchReturnLinesComponent, type ReturnLineSeries } from './golden-search-return-lines.component';

const DAY_1 = etMidnightMs('2025-03-03') + 16 * 3600_000;
const DAY_2 = etMidnightMs('2025-03-04') + 16 * 3600_000;

async function renderLines(series: ReturnLineSeries[]) {
  return render(GoldenSearchReturnLinesComponent, { inputs: { series, chartLabel: 'Development cumulative return' } });
}

describe('GoldenSearchReturnLinesComponent', () => {
  it('draws one line per candidate, names where each ends, and keeps a day a line has no value for as a dash', async () => {
    const view = await renderLines([
      { key: 'all_period', label: 'All-period fit', tone: 'primary', points: [{ ms: DAY_1, value: 0 }, { ms: DAY_2, value: 0.012 }] },
      { key: 'incumbent', label: 'Current settings', tone: 'benchmark', points: [{ ms: DAY_2, value: -0.004 }] },
    ]);

    expect(screen.getByRole('img', { name: 'Development cumulative return — ends at All-period fit +1.2%, Current settings -0.4%' })).not.toBeNull();
    expect(view.container.querySelectorAll('path.line--primary')).toHaveLength(1);
    expect(view.container.querySelectorAll('path.line--benchmark')).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: /show the daily values as a table/i }));
    const rows = within(screen.getByRole('table')).getAllByRole('row');
    expect(rows[1].textContent).toMatch(/2025-03-03\s*0\.0%\s*—/);
    expect(rows[2].textContent).toMatch(/2025-03-04\s*\+1\.2%\s*-0\.4%/);
  });

  it('says so when no run recorded a daily value, instead of drawing an empty chart', async () => {
    await renderLines([{ key: 'all_period', label: 'All-period fit', tone: 'primary', points: [] }]);

    expect(screen.queryByRole('img')).toBeNull();
    expect(screen.getByText('No daily values were recorded for these runs.')).not.toBeNull();
  });
});
