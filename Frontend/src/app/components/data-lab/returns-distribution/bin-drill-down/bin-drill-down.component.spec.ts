import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { DayReturns } from '../returns-distribution.service';
import { BinDrillDownComponent } from './bin-drill-down.component';

/** 09:30 ET anchors (13:30 UTC, EDT) so `etIsoDate` renders 2024-07-0x. */
const DAY_MS = (july: number) => Date.UTC(2024, 6, july, 13, 30);

const DAYS: DayReturns[] = [
  {
    sessionOpenMsUtc: DAY_MS(2),
    closeToClosePct: 0.25,
    sessionPct: 0.31,
    overnightPct: -0.06,
    preMarketPct: 0.02,
    morningPct: 0.19,
    afternoonPct: 0.12,
    afterHoursPct: null,
    volume: 4_500_000,
  },
  {
    sessionOpenMsUtc: DAY_MS(3),
    closeToClosePct: 0.4,
    sessionPct: 0.1,
    overnightPct: 0.3,
    preMarketPct: null,
    morningPct: -0.05,
    afternoonPct: 0.15,
    afterHoursPct: 0.01,
    volume: 3_000_000,
  },
  {
    sessionOpenMsUtc: DAY_MS(5),
    closeToClosePct: 1.2, // outside the [0, 0.5) basket
    sessionPct: 1.0,
    overnightPct: 0.2,
    preMarketPct: null,
    morningPct: 0.5,
    afternoonPct: 0.5,
    afterHoursPct: null,
    volume: 9_000_000,
  },
];

const BIN = { lowerPct: 0, upperPct: 0.5, count: 2, isEdge: false };

describe('BinDrillDownComponent', () => {
  it('lists only the days inside the basket, with the full segment breakdown', async () => {
    await render(BinDrillDownComponent, {
      componentInputs: { days: DAYS, bin: BIN, kind: 'close_to_close' },
    });

    expect(screen.getByText('Basket 0.0% to 0.5% — 2 day(s)')).toBeTruthy();
    expect(screen.getByText('2024-07-02')).toBeTruthy();
    expect(screen.getByText('2024-07-03')).toBeTruthy();
    expect(screen.queryByText('2024-07-05')).toBeNull();

    for (const label of ['Overnight gap', 'Pre-market', 'Morning', 'Afternoon', 'After hours']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText('no data').length).toBeGreaterThan(0); // after-hours / pre-market with no bars
    expect(screen.getByText(/4,500,000 shares/)).toBeTruthy();
  });

  it('emits the clicked day’s session anchor for the candle pane', async () => {
    const daySelected = vi.fn();
    const { fixture } = await render(BinDrillDownComponent, {
      componentInputs: { days: DAYS, bin: BIN, kind: 'close_to_close' },
    });
    fixture.componentInstance.daySelected.subscribe(daySelected);

    await userEvent.click(screen.getByRole('button', { name: /2024-07-02/ }));

    expect(daySelected).toHaveBeenCalledWith(DAY_MS(2));
  });
});
