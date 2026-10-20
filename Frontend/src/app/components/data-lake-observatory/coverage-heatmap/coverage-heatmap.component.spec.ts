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
  it('names every state by its ET trading date, gives each its own glyph, and passes AXE', async () => {
    // The 09:30 ET open is 13:30 UTC; the label is the contract that the
    // date is resolved in ET, never the viewer's own zone.
    const { container } = await renderHeatmap();

    expect
      .soft(screen.queryByRole('button', { name: 'SPY, 2026-05-18, Complete' }), 'names 2026-05-18 as Complete, by its ET trading date')
      .toBeTruthy();
    expect
      .soft(screen.queryByRole('button', { name: 'SPY, 2026-05-19, Fetching' }), 'names 2026-05-19 as Fetching, by its ET trading date')
      .toBeTruthy();
    expect
      .soft(screen.queryByRole('button', { name: 'SPY, 2026-05-20, Stale' }), 'names 2026-05-20 as Stale, by its ET trading date')
      .toBeTruthy();
    expect
      .soft(screen.queryByRole('button', { name: 'SPY, 2026-05-21, Failed' }), 'names 2026-05-21 as Failed, by its ET trading date')
      .toBeTruthy();
    expect
      .soft(screen.queryByRole('button', { name: 'SPY, 2026-05-22, Missing' }), 'names 2026-05-22 as Missing, by its ET trading date')
      .toBeTruthy();
    expect
      .soft(screen.queryByRole('button', { name: 'SPY, 2026-05-26, Needs Rebuild' }), 'names 2026-05-26 as Needs Rebuild, by its ET trading date')
      .toBeTruthy();

    const cells = Array.from(container.querySelectorAll('.cell'));
    const glyphs = cells.map((cell) => cell.textContent?.trim());
    expect.soft(new Set(glyphs).size, 'gives each state a glyph so it reads without colour').toBe(cells.length);

    // `region` is a harness artifact: the grid renders as a fragment without
    // the route shell that supplies its landmark. Contrast is fixed by the
    // token palette and checked visually, as elsewhere in this suite.
    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });

    expect.soft(results.violations, 'passes AXE').toEqual([]);
  });

  it.each<[string, string, object]>([
    [
      'a session a run refuses, with why',
      'SPY, 2026-05-26, Needs Rebuild',
      {
        symbol: 'SPY',
        cell: { artifactId: 15, status: 'needs_rebuild', rebuildReason: 'adjustment_record_missing' },
      },
    ],
    [
      'a session with nothing on disk: that is an answer too',
      'SPY, 2026-05-22, Missing',
      { symbol: 'SPY', cell: { artifactId: null, status: 'missing' } },
    ],
  ])('emits the clicked cell for %s', async (_name, button, selection) => {
    const view = await renderHeatmap();
    let emitted: CoverageCellSelection | null = null;
    view.fixture.componentInstance.cellSelected.subscribe((selected) => {
      emitted = selected;
    });

    fireEvent.click(screen.getByRole('button', { name: button }));

    expect(emitted).toMatchObject(selection);
  });
});
