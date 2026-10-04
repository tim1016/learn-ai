import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { buildCoverageBoard, type CoverageBoard } from '../lib/coverage-board';
import type {
  CoverageResponse,
  CoverageStatus,
  DataLakeRead,
  RebuildReason,
} from '../../../shared/data-lake';
import { CoverageHeatmapComponent, type CoverageCellSelection } from './coverage-heatmap.component';

/** 09:30 America/New_York on the given May 2026 date, as int64 ms UTC. */
function sessionOpenMs(day: number): number {
  return Date.UTC(2026, 4, day, 13, 30);
}

function coverageRead(
  symbol: string,
  days: readonly { day: number; status: CoverageStatus; artifactId?: number; reason?: RebuildReason }[],
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
        rebuild_reason: entry.reason ?? null,
      })),
    },
  };
}

const EVERY_STATE: CoverageBoard = buildCoverageBoard([
  {
    symbol: 'SPY',
    read: coverageRead('SPY', [
      { day: 18, status: 'complete', artifactId: 11 },
      { day: 19, status: 'fetching', artifactId: 12 },
      { day: 20, status: 'stale', artifactId: 13 },
      { day: 21, status: 'failed', artifactId: 14 },
      { day: 22, status: 'missing' },
      { day: 26, status: 'needs_rebuild', artifactId: 15, reason: 'adjustment_record_missing' },
    ]),
  },
]);

async function renderHeatmap(board: CoverageBoard = EVERY_STATE) {
  return render(CoverageHeatmapComponent, { componentInputs: { board } });
}

describe('CoverageHeatmapComponent', () => {
  it.each([
    ['2026-05-18', 'Complete'],
    ['2026-05-19', 'Fetching'],
    ['2026-05-20', 'Stale'],
    ['2026-05-21', 'Failed'],
    ['2026-05-22', 'Missing'],
    ['2026-05-26', 'Needs Rebuild'],
  ])('names %s as %s, by its ET trading date', async (date, status) => {
    // The 09:30 ET open is 13:30 UTC; the label is the contract that the
    // date is resolved in ET, never the viewer's own zone.
    await renderHeatmap();

    expect(screen.getByRole('button', { name: `SPY, ${date}, ${status}` })).toBeTruthy();
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

  it('emits the clicked cell, with why a run refuses it', async () => {
    const view = await renderHeatmap();
    let emitted: CoverageCellSelection | null = null;
    view.fixture.componentInstance.cellSelected.subscribe((selection) => {
      emitted = selection;
    });

    fireEvent.click(screen.getByRole('button', { name: 'SPY, 2026-05-26, Needs Rebuild' }));

    expect(emitted).toMatchObject({
      symbol: 'SPY',
      cell: { artifactId: 15, status: 'needs_rebuild', rebuildReason: 'adjustment_record_missing' },
    });
  });

  it('lets a session with nothing on disk be picked: that is an answer too', async () => {
    const view = await renderHeatmap();
    let emitted: CoverageCellSelection | null = null;
    view.fixture.componentInstance.cellSelected.subscribe((selection) => {
      emitted = selection;
    });

    fireEvent.click(screen.getByRole('button', { name: 'SPY, 2026-05-22, Missing' }));

    expect(emitted).toMatchObject({ symbol: 'SPY', cell: { artifactId: null, status: 'missing' } });
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
