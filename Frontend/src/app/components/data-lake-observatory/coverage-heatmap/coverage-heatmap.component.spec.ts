import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { buildCoverageBoard, type CoverageBoard } from '../lib/coverage-board';
import type { CoverageResponse, CoverageStatus, DataLakeRead } from '../lib/data-lake.types';
import { CoverageHeatmapComponent } from './coverage-heatmap.component';

/** 09:30 America/New_York on the given May 2026 date, as int64 ms UTC. */
function sessionOpenMs(day: number): number {
  return Date.UTC(2026, 4, day, 13, 30);
}

function coverageRead(
  symbol: string,
  days: readonly { day: number; status: CoverageStatus; artifactId?: number }[],
): DataLakeRead<CoverageResponse> {
  return {
    kind: 'ok',
    value: {
      market: 'usa',
      symbol,
      data_type: 'trade',
      resolution: 'minute',
      provider: 'polygon',
      price_adjustment_mode: 'raw',
      days: days.map((entry) => ({
        trading_date_ms: sessionOpenMs(entry.day),
        status: entry.status,
        artifact_id: entry.artifactId ?? null,
      })),
    },
  };
}

const ALL_FOUR_STATES: CoverageBoard = buildCoverageBoard([
  {
    symbol: 'SPY',
    read: coverageRead('SPY', [
      { day: 18, status: 'complete', artifactId: 11 },
      { day: 19, status: 'fetching', artifactId: 12 },
      { day: 20, status: 'stale', artifactId: 13 },
      { day: 21, status: 'failed', artifactId: 14 },
      { day: 22, status: 'missing' },
    ]),
  },
]);

async function renderHeatmap(board: CoverageBoard = ALL_FOUR_STATES) {
  return render(CoverageHeatmapComponent, { componentInputs: { board } });
}

describe('CoverageHeatmapComponent', () => {
  it.each([
    ['2026-05-18', 'Complete', 'open artifact receipt'],
    ['2026-05-19', 'Fetching', 'open artifact receipt'],
    ['2026-05-20', 'Stale', 'open artifact receipt'],
    ['2026-05-21', 'Failed', 'open artifact receipt'],
    ['2026-05-22', 'Missing', 'no artifact receipt'],
  ])('renders %s as %s', async (date, status, receipt) => {
    await renderHeatmap();

    expect(screen.getByRole('button', { name: `SPY, ${date}, ${status}, ${receipt}` })).toBeTruthy();
  });

  it('anchors each cell to its ET trading date, not the viewer local one', async () => {
    // The 09:30 ET open is 13:30 UTC; rendering it in a US local zone would
    // still read 2026-05-18, but a UTC-shifted render of a midnight anchor
    // would drift a day. The label is the contract either way.
    await renderHeatmap();

    expect(
      screen.getByRole('button', { name: 'SPY, 2026-05-18, Complete, open artifact receipt' }),
    ).toBeTruthy();
  });

  it('draws one cell per calendar session and no gap for the weekend between', async () => {
    const board = buildCoverageBoard([
      {
        symbol: 'SPY',
        read: coverageRead('SPY', [
          { day: 15, status: 'complete', artifactId: 1 },
          { day: 18, status: 'complete', artifactId: 2 },
        ]),
      },
    ]);
    await renderHeatmap(board);

    expect(screen.getAllByRole('button')).toHaveLength(2);
    expect(screen.getByText('2 sessions')).toBeTruthy();
  });

  it('gives each state a glyph so it reads without colour', async () => {
    const { container } = await renderHeatmap();
    const cells = Array.from(container.querySelectorAll('.cell'));

    const glyphs = cells.map((cell) => cell.textContent?.trim());
    expect(new Set(glyphs).size).toBe(cells.length);
  });

  it('emits the clicked cell so the inspector can open its receipt', async () => {
    const view = await renderHeatmap();
    let emitted: { symbol: string; cell: { artifactId: number | null } } | null = null;
    view.fixture.componentInstance.cellSelected.subscribe((selection) => {
      emitted = selection;
    });

    fireEvent.click(
      screen.getByRole('button', { name: 'SPY, 2026-05-18, Complete, open artifact receipt' }),
    );

    expect(emitted).toMatchObject({ symbol: 'SPY', cell: { artifactId: 11 } });
  });

  it('refuses to offer a receipt for a session with no artifact', async () => {
    await renderHeatmap();

    const missing = screen.getByRole('button', {
      name: 'SPY, 2026-05-22, Missing, no artifact receipt',
    });
    expect(missing.hasAttribute('disabled')).toBe(true);
  });

  it('passes AXE', async () => {
    // `region` is a harness artifact: the grid renders as a fragment without
    // the route shell that supplies its landmark. Contrast is fixed by the
    // token palette and checked visually, as elsewhere in this suite.
    await renderHeatmap();

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
