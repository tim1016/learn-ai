import { fireEvent, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { RunDetailRefusedError } from '../run-detail.service';
import type { SessionDetail } from '../run-detail.types';
import { CALL_585, PUT_585, sessionDetail, shownSeries, skippedSession, TUESDAY_OPEN, withheldSeries } from '../testing/detail-fixtures';
import { fakeRunDetail } from '../testing/fake-run-detail';
import { renderSession } from '../testing/render-session';

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: vi.fn(),
  LineSeries: 'LineSeries',
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

async function violations(): Promise<string[]> {
  const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
  return results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.html).join(' | ')}`);
}

const asked = { openMs: TUESDAY_OPEN, origin: null };
const serving = (day: SessionDetail) => fakeRunDetail(vi, { session: vi.fn(async () => day) });

describe('one day for a screen reader', () => {
  it('has no violation before a day is chosen', async () => {
    await renderSession(vi);

    expect(await violations()).toEqual([]);
  });

  it('has no violation while a day is being read', async () => {
    const waiting = new Promise<never>(() => undefined);
    await renderSession(vi, { detail: fakeRunDetail(vi, { session: vi.fn(() => waiting) }), request: asked });
    await screen.findByText('Reading the day…');

    expect(await violations()).toEqual([]);
  });

  it('has no violation with a traded day, its chart, its legend, its lists and a contract’s code shown', async () => {
    await renderSession(vi, { detail: serving(sessionDetail()), request: asked });
    await screen.findByRole('img');
    fireEvent.focus(screen.getAllByLabelText(/SPY.*Call/)[0]);

    expect(await violations()).toEqual([]);
  });

  it('has no violation with a series withheld and the setup unreadable', async () => {
    const day = sessionDetail({
      setup_cannot_open_reason: 'lots: Extra inputs are not permitted.',
      contracts: [withheldSeries(CALL_585.symbol, CALL_585, 'changed'), shownSeries(PUT_585.symbol, PUT_585, [[3, '1.40'], [4, '1.44']])],
    });
    await renderSession(vi, { detail: serving(day), request: asked });
    await screen.findByRole('region', { name: 'Not drawn' });

    expect(await violations()).toEqual([]);
  });

  it('has no violation with a skipped day', async () => {
    await renderSession(vi, { detail: serving(skippedSession()), request: asked });
    await screen.findByRole('region', { name: 'Not drawn' });

    expect(await violations()).toEqual([]);
  });

  it('has no violation with the server’s refusal', async () => {
    const refused = fakeRunDetail(vi, { session: vi.fn(async () => { throw new RunDetailRefusedError('X', 'Run 7 did not test that day.', 404); }) });
    await renderSession(vi, { detail: refused, request: asked });
    await screen.findByRole('alert');

    expect(await violations()).toEqual([]);
  });
});
