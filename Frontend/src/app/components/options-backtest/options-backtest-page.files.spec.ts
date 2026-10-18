import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import { SetupInvalidError } from './options-backtest.service';
import type { SavedSetup, SetupRequest, SetupSummary } from './options-backtest.types';
import { deferred } from './past-chain/testing/deferred';
import { EXAMPLE_ONE_REENTRY, exampleBody, readBackOf, servedExamples, servedSaved, summaryFor } from './testing/fixtures';
import { MAX_SETUP_FILE_BYTES } from './setup-file';
import { lettingGo, press, typeInto } from './testing/page-actions';
import { fakeService, renderPage, sentencesFor, type FakeDownload } from './testing/render-page';

function readText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

/** jsdom's File has no `text()`; a browser's does. */
beforeAll(() => {
  if (typeof File.prototype.text !== 'function') {
    File.prototype.text = function (this: Blob): Promise<string> {
      return readText(this);
    };
  }
});

const download = (): FakeDownload => ({ save: vi.fn() });
const sideList = () => screen.getByRole('complementary', { name: 'Saved setups' });
const exportButton = () => screen.getByRole('button', { name: 'Export setup' }) as HTMLButtonElement;
const chooser = () => screen.getByLabelText('Setup file to import');

async function openExample(): Promise<void> {
  fireEvent.click(await within(sideList()).findByRole('button', { name: new RegExp(`^${EXAMPLE_ONE_REENTRY}`) }));
  await screen.findByRole('article', { name: 'Leg 2' });
}

function chooseFile(contents: string, name = 'straddle.json'): void {
  fireEvent.change(chooser(), { target: { files: [new File([contents], name, { type: 'application/json' })] } });
}

describe('export', () => {
  it('is not offered for a setup that has not been read back as complete', async () => {
    await renderPage(fakeService(vi), download());

    expect(exportButton().disabled).toBe(true);
  });

  it('downloads the setup as the server read it back, which is what the routes accept, named for the setup', async () => {
    const files = download();
    await renderPage(fakeService(vi), files);
    await openExample();
    typeInto('Setup name', 'My straddle');
    await waitFor(() => expect(exportButton().disabled).toBe(false));

    press('Export setup');

    expect(files.save).toHaveBeenCalledTimes(1);
    const [blob, filename] = files.save.mock.calls[0] ?? [];
    expect(filename).toBe('My straddle.json');
    expect(blob?.type).toBe('application/json');
    const text = blob === undefined ? undefined : await readText(blob);
    expect(text).toBe(`${JSON.stringify(readBackOf(exampleBody()), null, 2)}\n`);
    expect(JSON.parse(text ?? '')).toEqual(readBackOf(exampleBody()));
  });

  it('uses the generated setup name when exporting a new setup', async () => {
    const files = download();
    await renderPage(fakeService(vi), files);
    await openExample();
    await waitFor(() => expect(exportButton().disabled).toBe(false));

    press('Export setup');

    expect(files.save.mock.calls[0]?.[1]).toBe('SPY, Buy 1 ATM call and Buy 1 ATM put, 0935-1514 ET.json');
  });

  it('goes stale with the setup: after an edit it waits for the server to read the new body', async () => {
    await renderPage(fakeService(vi), download());
    await openExample();
    await waitFor(() => expect(exportButton().disabled).toBe(false));

    typeInto('Slippage (percent)', 'abc');

    expect(exportButton().disabled).toBe(true);
  });
});

describe('import', () => {
  it('loads a file the server accepts, and sends the server the file as it was written', async () => {
    const service = fakeService(vi);
    await renderPage(service, download());
    const written = { ...exampleBody(), underlying: 'AAPL', slippage_percent: 0.45 };

    chooseFile(JSON.stringify(written));

    await waitFor(() => expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toContain('AAPL, Buy'));
    await screen.findByRole('article', { name: 'Leg 2' });
    expect(service.describe).toHaveBeenCalledWith(written);
    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.45');
    expect(screen.getByText(/straddle\.json was loaded/)).toBeTruthy();
  });

  it('loads a number the file wrote for a decimal as the digits the server read back', async () => {
    await renderPage(fakeService(vi), download());

    chooseFile(JSON.stringify({ ...exampleBody(), slippage_percent: 0.25, starting_cash_usd: 5000, fees: { kind: 'per_contract', per_contract_usd: 0.65 } }));

    await screen.findByRole('article', { name: 'Leg 2' });
    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.25');
    expect((screen.getByLabelText('Cash') as HTMLInputElement).value).toBe('5000');
    expect((screen.getByLabelText('Dollars per contract') as HTMLInputElement).value).toBe('0.65');
  });

  it('loads what the file left out from the server’s read-back, and fills in no default of its own', async () => {
    const { slippage_percent: _slippage, price_step_usd: _step, version: _version, ...bare } = exampleBody();
    const service = fakeService(vi, {
      describe: vi.fn(async (body: unknown) => {
        const read = sentencesFor(body as SetupRequest);
        return { ...read, setup: { ...read.setup, slippage_percent: '0.42' } };
      }),
    });
    await renderPage(service, download());

    chooseFile(JSON.stringify(bare));

    await screen.findByRole('article', { name: 'Leg 2' });
    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.42');
    expect(screen.getByText(/straddle\.json was loaded/)).toBeTruthy();
  });

  it('reads the times of an imported file back as clock times', async () => {
    await renderPage(fakeService(vi), download());

    chooseFile(JSON.stringify(exampleBody()));

    await screen.findByRole('article', { name: 'Leg 2' });
    expect((screen.getByLabelText('Entry time') as HTMLInputElement).value).toBe('09:35');
  });

  it('shows the server’s reasons for a file that is not a setup, and changes nothing', async () => {
    const service = fakeService(vi, {
      describe: vi.fn(async (body: unknown) => {
        if ((body as { underlying?: string }).underlying === 'bad') {
          throw new SetupInvalidError([{ loc: ['body', 'legs', 0, 'contracts'], msg: 'Input should be at most 1000' }, { loc: ['body', 'square_off'], msg: 'Input should be partial or complete' }]);
        }
        return summaryFor('w');
      }),
    });
    await renderPage(service, download());
    const before = (screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value;

    chooseFile(JSON.stringify({ ...exampleBody(), underlying: 'bad', slippage_percent: '9' }), 'broken.json');

    const notice = await screen.findByText(/broken\.json was not loaded/);
    const box = notice.closest('[role="alert"]') as HTMLElement;
    expect(within(box).getByText('legs.0.contracts: Input should be at most 1000')).toBeTruthy();
    expect(within(box).getByText('square_off: Input should be partial or complete')).toBeTruthy();
    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe(before);
    expect(screen.queryByRole('article', { name: 'Leg 1' })).toBeNull();
  });

  it('gives the server’s reason, as it says it for the whole setup, for a file whose window can hold no trade', async () => {
    await renderPage(fakeService(vi), download());

    chooseFile(JSON.stringify({ ...exampleBody(), entry: { anchor: 'open', offset_ms: 0 } }), 'early.json');

    const notice = await screen.findByText(/early\.json was not loaded/);
    const box = notice.closest('[role="alert"]') as HTMLElement;
    expect(within(box).getByText('the entry time must be at least one minute after the open')).toBeTruthy();
    expect(screen.queryByRole('article', { name: 'Leg 1' })).toBeNull();
  });

  it('refuses a file over a megabyte before it is read or sent, and says why', async () => {
    const service = fakeService(vi);
    await renderPage(service, download());
    await waitFor(() => expect(service.describe).toHaveBeenCalled());
    const calls = service.describe.mock.calls.length;
    const read = vi.spyOn(File.prototype, 'text');
    const huge = new File([new Uint8Array(MAX_SETUP_FILE_BYTES + 1)], 'huge.json', { type: 'application/json' });

    fireEvent.change(chooser(), { target: { files: [huge] } });

    const notice = await screen.findByText(/huge\.json was not loaded/);
    expect(notice.closest('[role="alert"]')?.textContent).toMatch(/1 MB/);
    expect(read).not.toHaveBeenCalled();
    expect(service.describe.mock.calls.length).toBe(calls);
    read.mockRestore();
  });

  it('is not undone by a save that finishes afterwards: that save does not re-select its row or name', async () => {
    let finishSave: () => void = () => undefined;
    const mine: SavedSetup = servedSaved('Mine', exampleBody());
    const list = vi.fn(async () => [...servedExamples(), mine]);
    const service = fakeService(vi, {
      list,
      save: vi.fn(
        (name: string) =>
          new Promise<SavedSetup>((resolve) => {
            finishSave = () => resolve({ ...mine, name });
          }),
      ),
    });
    await renderPage(service, download());
    await openExample();
    typeInto('Setup name', 'Mine');
    const save = screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement;
    await waitFor(() => expect(save.disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(service.save).toHaveBeenCalledTimes(1));

    chooseFile(JSON.stringify({ ...exampleBody(), underlying: 'AAPL' }), 'other.json');
    await screen.findByText(/other\.json was loaded/);
    finishSave();

    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    const row = await within(sideList()).findByRole('button', { name: /^Mine/ });
    expect(row.getAttribute('aria-current')).toBeNull();
    expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toContain('AAPL, Buy');
    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.3');
  });

  // The server's reading of a file takes as long as the data plane is busy: an earlier file can be read back after a later one.
  it('lets the last file chosen win when an earlier one is read back more slowly', async () => {
    const slow = deferred<SetupSummary>();
    const first = { ...exampleBody(), slippage_percent: 0.45 };
    const second = { ...exampleBody(), slippage_percent: 0.55 };
    const service = fakeService(vi, {
      describe: vi.fn((body: unknown) => ((body as SetupRequest).slippage_percent === 0.45 ? slow.promise : Promise.resolve(sentencesFor(body as SetupRequest)))),
    });
    await renderPage(service, download());
    chooseFile(JSON.stringify(first), 'first.json');
    await waitFor(() => expect(service.describe).toHaveBeenCalledWith(first));

    chooseFile(JSON.stringify(second), 'second.json');
    await screen.findByText(/second\.json was loaded/);
    slow.resolve(sentencesFor(first));
    for (let settled = 0; settled < 5; settled += 1) await lettingGo();

    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.55');
    expect(screen.queryByText(/first\.json was loaded/)).toBeNull();
    expect(screen.getByText(/second\.json was loaded/)).toBeTruthy();
  });

  it('is not applied over a saved setup that was opened while the file was being read back', async () => {
    const slow = deferred<SetupSummary>();
    const late = { ...exampleBody(), slippage_percent: 0.45 };
    const service = fakeService(vi, {
      describe: vi.fn((body: unknown) => ((body as SetupRequest).slippage_percent === 0.45 ? slow.promise : Promise.resolve(sentencesFor(body as SetupRequest)))),
    });
    await renderPage(service, download());
    chooseFile(JSON.stringify(late), 'late.json');
    await waitFor(() => expect(service.describe).toHaveBeenCalledWith(late));

    await openExample();
    slow.resolve(sentencesFor(late));
    for (let settled = 0; settled < 5; settled += 1) await lettingGo();

    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.3');
    expect(screen.queryByText(/late\.json was loaded/)).toBeNull();
  });

  it('says so, and changes nothing, for a file that is not JSON', async () => {
    const service = fakeService(vi);
    await renderPage(service, download());
    await waitFor(() => expect(service.describe).toHaveBeenCalled());
    const calls = service.describe.mock.calls.length;

    chooseFile('{ this is not json', 'notes.json');

    const notice = await screen.findByText(/notes\.json was not loaded/);
    expect(notice.closest('[role="alert"]')?.textContent).toMatch(/not JSON/);
    expect(service.describe.mock.calls.length).toBe(calls);
  });
});
