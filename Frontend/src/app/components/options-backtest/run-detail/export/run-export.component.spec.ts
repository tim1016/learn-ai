import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { FileDownloadService } from '../../../../shared/file/file-download.service';
import { reads } from '../../testing/page-actions';
import { RunDetailRefusedError, RunDetailService } from '../run-detail.service';
import type { CsvDownload } from '../run-detail.types';
import { fakeRunDetail, type FakeRunDetail } from '../testing/fake-run-detail';
import { RunExportComponent } from './run-export.component';

const NOTE = 'The file holds every trade of the run, not only this page, and its readable time column is New York time.';

async function renderExport(detail: FakeRunDetail, runId = 7) {
  const save = vi.fn();
  const view = await render(RunExportComponent, {
    inputs: { runId },
    providers: [
      { provide: RunDetailService, useValue: detail },
      { provide: FileDownloadService, useValue: { save } },
    ],
  });
  return { ...view, save };
}

const button = () => screen.getByRole<HTMLButtonElement>('button', { name: /^(Export trades \(CSV\)|Exporting…)$/ });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

describe('the export of every trade', () => {
  it('says beside the button, in one line, that the file holds every trade and that its readable time is New York time', async () => {
    await renderExport(fakeRunDetail(vi));

    expect(screen.getByText(NOTE)).toBeTruthy();
    expect(button().getAttribute('aria-describedby')).toBe(screen.getByText(NOTE).id);
  });

  it('saves the bytes the server sent, the very ones, under the name the server gave them', async () => {
    const file: CsvDownload = { blob: new Blob(['unix_ts\r\n1\r\n'], { type: 'text/csv' }), filename: 'options-backtest-run-7-trades.csv' };
    const detail = fakeRunDetail(vi, { tradesCsv: vi.fn(async () => file) });
    const { save } = await renderExport(detail);

    fireEvent.click(button());

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(detail.tradesCsv).toHaveBeenCalledWith(7);
    expect(save.mock.calls[0][0]).toBe(file.blob);
    expect(save.mock.calls[0][1]).toBe('options-backtest-run-7-trades.csv');
  });

  it('names the file for the run when the server named none', async () => {
    const detail = fakeRunDetail(vi, { tradesCsv: vi.fn(async () => ({ blob: new Blob(['x']), filename: null })) });
    const { save } = await renderExport(detail);

    fireEvent.click(button());

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save.mock.calls[0][1]).toBe('options-backtest-trades.csv');
  });

  it('is busy while the file comes: the button says so, is disabled, and a second press asks for nothing', async () => {
    const waiting = deferred<CsvDownload>();
    const detail = fakeRunDetail(vi, { tradesCsv: vi.fn(() => waiting.promise) });
    const { save } = await renderExport(detail);

    fireEvent.click(button());
    await screen.findByRole('button', { name: 'Exporting…' });

    expect(button().disabled).toBe(true);
    expect(button().getAttribute('aria-busy')).toBe('true');
    fireEvent.click(button());
    expect(detail.tradesCsv).toHaveBeenCalledTimes(1);
    waiting.resolve({ blob: new Blob(['x']), filename: 'a.csv' });
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    await screen.findByRole('button', { name: 'Export trades (CSV)' });
    expect(button().disabled).toBe(false);
    expect(button().getAttribute('aria-busy')).toBe('false');
  });

  it('shows the server’s words when it refuses, saves nothing, and is ready to try again', async () => {
    const detail = fakeRunDetail(vi, {
      tradesCsv: vi.fn(async () => {
        throw new RunDetailRefusedError('OPTIONS_BACKTEST_RUN_NOT_COMPLETE', 'Run 7 is failed. Only a complete run has trades and sessions.', 409);
      }),
    });
    const { save } = await renderExport(detail);

    fireEvent.click(button());

    expect(reads(await screen.findByRole('alert'))).toBe('Run 7 is failed. Only a complete run has trades and sessions.');
    expect(save).not.toHaveBeenCalled();
    expect(button().disabled).toBe(false);
  });

  it('says plainly when the file could not be made for another reason', async () => {
    const detail = fakeRunDetail(vi, { tradesCsv: vi.fn(async () => { throw new Error('offline'); }) });
    await renderExport(detail);

    fireEvent.click(button());

    expect(reads(await screen.findByRole('alert'))).toBe('The trades file could not be made. Try again.');
  });

  it('clears an earlier failure when it is tried again', async () => {
    let refusing = true;
    const detail = fakeRunDetail(vi, {
      tradesCsv: vi.fn(async () => {
        if (refusing) throw new Error('offline');
        return { blob: new Blob(['x']), filename: 'a.csv' };
      }),
    });
    const { save } = await renderExport(detail);
    fireEvent.click(button());
    await screen.findByRole('alert');

    refusing = false;
    fireEvent.click(button());

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('does not show one run’s failure under another run', async () => {
    const detail = fakeRunDetail(vi, { tradesCsv: vi.fn(async () => { throw new Error('offline'); }) });
    const { rerender } = await renderExport(detail, 7);
    fireEvent.click(button());
    await screen.findByRole('alert');

    await rerender({ inputs: { runId: 8 } });

    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('does not stay busy for another run because the run before it is still downloading', async () => {
    const waiting = deferred<CsvDownload>();
    const detail = fakeRunDetail(vi, { tradesCsv: vi.fn(() => waiting.promise) });
    const { rerender } = await renderExport(detail, 7);
    fireEvent.click(button());
    await screen.findByRole('button', { name: 'Exporting…' });

    await rerender({ inputs: { runId: 8 } });

    expect(button().disabled).toBe(false);
    expect(reads(button())).toBe('Export trades (CSV)');
  });
});
