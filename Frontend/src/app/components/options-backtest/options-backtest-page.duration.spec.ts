import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import { dataCheck, sessionCheck } from './testing/fixtures';
import { checkButton, dateFields, openExample, pickDates, press, typeInto } from './testing/page-actions';
import { fakeService, renderPage, type FakeDownload } from './testing/render-page';

/** jsdom's File and Blob have no `text()`; a browser's do. */
function readText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

beforeAll(() => {
  if (typeof File.prototype.text !== 'function') {
    File.prototype.text = function (this: Blob): Promise<string> {
      return readText(this);
    };
  }
});

const download = (): FakeDownload => ({ save: vi.fn() });

describe('the Duration card', () => {
  it('sits between Costs and the plain-words summary', async () => {
    await renderPage(fakeService(vi));

    const headings = screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent);

    expect(headings.slice(headings.indexOf('Costs'), headings.indexOf('In plain words') + 1)).toEqual(['Costs', 'Duration', 'In plain words']);
  });

  it('takes its dates from the shared picker, and has no plain date input anywhere', async () => {
    await renderPage(fakeService(vi));

    const card = screen.getByRole('region', { name: 'Duration' });

    expect(within(card).getByRole('group', { name: 'Backtest dates' })).toBeTruthy();
    expect(within(card).getByLabelText('Start date')).toBeTruthy();
    expect(within(card).getByLabelText('End date')).toBeTruthy();
    expect([...document.querySelectorAll('input')].filter((field) => field.type === 'date')).toEqual([]);
  });

  it('offers quick ranges, and none that reach further back than the options plan', async () => {
    await renderPage(fakeService(vi));

    const ranges = within(screen.getByRole('group', { name: 'Quick ranges' }));

    expect(ranges.getByRole('button', { name: /^1M/ })).toBeTruthy();
    expect(ranges.queryByRole('button', { name: /^5Y/ })).toBeNull();
    expect(ranges.queryByRole('button', { name: /^Max/ })).toBeNull();
  });

  it('starts with no dates, and does not choose any', async () => {
    await renderPage(fakeService(vi));

    expect(dateFields()).toEqual({ from: '', to: '' });
  });
});

describe('the dates are not part of the setup', () => {
  it('reach no request that describes, saves or exports a setup', async () => {
    const files = download();
    const service = fakeService(vi);
    await renderPage(service, files);
    await openExample();

    pickDates('2026-09-14', '2026-09-18');
    expect(dateFields()).toEqual({ from: '2026-09-14', to: '2026-09-18' });
    typeInto('Setup name', 'Mine');
    typeInto('Slippage (percent)', '0.4');
    await waitFor(() => expect(service.describe).toHaveBeenLastCalledWith(expect.objectContaining({ slippage_percent: '0.4' })));
    const saveButton = screen.getByRole<HTMLButtonElement>('button', { name: 'Save setup' });
    await waitFor(() => expect(saveButton.disabled).toBe(false));
    press('Save setup');
    press('Export setup');

    await waitFor(() => expect(service.save).toHaveBeenCalledTimes(1));
    const everything = [...service.describe.mock.calls, ...service.save.mock.calls].map((call) => JSON.stringify(call)).join('\n');
    const [blob] = files.save.mock.calls[0] ?? [];
    const exported = blob === undefined ? '' : await readText(blob);
    for (const text of [everything, exported]) {
      expect(text).not.toContain('2026-09');
      expect(text).not.toContain('1789358400000');
      expect(text).not.toContain('start_ms');
      expect(text).not.toContain('end_ms');
    }
  });

  it('are not touched by opening another setup', async () => {
    await renderPage(fakeService(vi));
    pickDates('2026-09-14', '2026-09-18');

    await openExample();

    expect(dateFields()).toEqual({ from: '2026-09-14', to: '2026-09-18' });
  });
});

describe('the line beside the dates, and the lower bound', () => {
  it('says that Check data tells from when option prices are available, until it has run', async () => {
    await renderPage(fakeService(vi));

    expect(screen.getByText('From when option prices are available is shown once Check data has run.')).toBeTruthy();
  });

  it('then says the day, in New York dates, as the server gave it', async () => {
    await renderPage(fakeService(vi));
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));

    fireEvent.click(checkButton());

    const line = await screen.findByText(/Option prices are available from/);
    expect(line.textContent?.replace(/\s+/g, ' ').trim()).toBe('Option prices are available from 2025-10-15');
    expect(line.querySelector('app-timestamp-display')?.getAttribute('data-timestamp-mode')).toBe('date-et');
  });

  it('is the day before in New York when the instant is late in the evening there', async () => {
    const service = fakeService(vi, {
      // 2025-10-15 02:00 UTC is 2025-10-14 22:00 in New York.
      checkData: vi.fn(async () => dataCheck([sessionCheck('2026-09-14')], { option_prices_from_ms: Date.UTC(2025, 9, 15, 2) })),
    });
    await renderPage(service);
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));

    fireEvent.click(checkButton());

    const line = await screen.findByText(/Option prices are available from/);
    expect(line.textContent?.replace(/\s+/g, ' ').trim()).toBe('Option prices are available from 2025-10-14');
  });

  it('refuses a start date before that day, with the reason in words, and the run waits for a date', async () => {
    await renderPage(fakeService(vi));
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    fireEvent.click(checkButton());
    await screen.findByText(/Option prices are available from/);

    pickDates('2025-10-14', '2026-09-18');

    expect(await screen.findByText('Option prices are available from 2025-10-15, so no earlier day can be picked.')).toBeTruthy();
    await waitFor(() => expect(checkButton().disabled).toBe(true));
    expect(screen.getByText('Check data is available once both dates are picked.')).toBeTruthy();
  });

  it('takes the day itself', async () => {
    await renderPage(fakeService(vi));
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    fireEvent.click(checkButton());
    await screen.findByText(/Option prices are available from/);

    pickDates('2025-10-15', '2026-09-18');

    await waitFor(() => expect(dateFields()).toEqual({ from: '2025-10-15', to: '2026-09-18' }));
    expect(checkButton().disabled).toBe(false);
  });
});
