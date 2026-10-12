import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { runDetail, runWithoutSetup } from '../../testing/run-fixtures';
import { RunDetailRefusedError, RunDetailService } from '../run-detail.service';
import { tradesPage } from '../testing/detail-fixtures';
import { fakeRunDetail } from '../testing/fake-run-detail';
import { RunTradesComponent } from './run-trades.component';

async function violations(): Promise<string[]> {
  const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
  return results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.html).join(' | ')}`);
}

describe('the trades of a run for a screen reader', () => {
  it('has no violation with the four trades, a mark among them, and the pager', async () => {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage({ total: 250, next_offset: 100 })) });
    await render(RunTradesComponent, { inputs: { run: runDetail() }, providers: [{ provide: RunDetailService, useValue: detail }] });
    await screen.findByRole('table', { name: 'Leg trades' });
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByRole('table', { name: 'Leg trades' });

    expect(await violations()).toEqual([]);
  });

  it('has no violation with the leg typed in because the setup cannot be read', async () => {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) });
    await render(RunTradesComponent, { inputs: { run: runWithoutSetup() }, providers: [{ provide: RunDetailService, useValue: detail }] });
    await screen.findByRole('table', { name: 'Leg trades' });

    expect(await violations()).toEqual([]);
  });

  it('has no violation while the trades are being read', async () => {
    const waiting = new Promise<never>(() => undefined);
    await render(RunTradesComponent, { inputs: { run: runDetail() }, providers: [{ provide: RunDetailService, useValue: fakeRunDetail(vi, { trades: vi.fn(() => waiting) }) }] });
    await screen.findByText('Reading the trades…');

    expect(await violations()).toEqual([]);
  });

  it('has no violation when the run made no trades', async () => {
    await render(RunTradesComponent, { inputs: { run: runDetail() }, providers: [{ provide: RunDetailService, useValue: fakeRunDetail(vi) }] });
    await screen.findByText('This run made no trades.');

    expect(await violations()).toEqual([]);
  });

  it('has no violation with the server’s refusal showing', async () => {
    const refused = fakeRunDetail(vi, { trades: vi.fn(async () => { throw new RunDetailRefusedError('X', 'Run 7 is failed.', 409); }) });
    await render(RunTradesComponent, { inputs: { run: runDetail() }, providers: [{ provide: RunDetailService, useValue: refused }] });
    await screen.findByRole('alert');

    expect(await violations()).toEqual([]);
  });
});
