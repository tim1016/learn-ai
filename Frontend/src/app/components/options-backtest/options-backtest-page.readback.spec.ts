import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { SavedSetup, SetupRequest, SetupSummary } from './options-backtest.types';
import { EXAMPLE_ONE_REENTRY, exampleBody, readBackOf, servedSaved } from './testing/fixtures';
import { typeInto } from './testing/page-actions';
import { fakeService, renderPage, sentencesFor, type FakeDownload } from './testing/render-page';

/**
 * Save and Export send only a setup the server has read back, and Save goes
 * once however it is asked for. The gap tests act in the same tick as an edit,
 * before the page has read the edit back: testing-library's events each run
 * change detection afterwards, which would close that gap, so these use plain
 * DOM events.
 */

const sideList = () => screen.getByRole('complementary', { name: 'Saved setups' });
const saveButton = () => screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement;
const exportButton = () => screen.getByRole('button', { name: 'Export setup' }) as HTMLButtonElement;
const nameField = () => screen.getByLabelText<HTMLInputElement>('Setup name');
const download = (): FakeDownload => ({ save: vi.fn() });

/** jsdom's Blob has no `text()`; a browser's does. */
function readText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

async function openExample(): Promise<void> {
  fireEvent.click(await within(sideList()).findByRole('button', { name: new RegExp(`^${EXAMPLE_ONE_REENTRY}`) }));
  await screen.findByRole('article', { name: 'Leg 2' });
}

/** Names the open example and waits until the server has read it back, so that Save and Export are on. */
async function readyToSave(name: string): Promise<void> {
  await openExample();
  typeInto('Setup name', name);
  await waitFor(() => expect(saveButton().disabled).toBe(false));
  await waitFor(() => expect(exportButton().disabled).toBe(false));
}

function editWithoutLettingThePageCatchUp(label: string, value: string): void {
  const field = screen.getByLabelText<HTMLInputElement>(label);
  field.value = value;
  field.dispatchEvent(new Event('input', { bubbles: true }));
}

const pressEnterInTheName = () => nameField().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));

function savedAs(name: string): SavedSetup {
  return servedSaved(name, exampleBody());
}

describe('Save and Export wait for the server to read the setup back', () => {
  it('both stay off while an edited setup is being read back, and come on when it has been', async () => {
    let hold = false;
    const answers: (() => void)[] = [];
    const service = fakeService(vi, {
      describe: vi.fn((body: unknown) => {
        const setup = body as SetupRequest;
        if (!hold) return Promise.resolve(sentencesFor(setup));
        return new Promise<SetupSummary>((resolve) => answers.push(() => resolve(sentencesFor(setup))));
      }),
    });
    await renderPage(service, download());
    await readyToSave('Mine');
    hold = true;

    typeInto('Slippage (percent)', '0.5');

    await waitFor(() => expect(service.describe).toHaveBeenLastCalledWith(expect.objectContaining({ slippage_percent: '0.5' })));
    expect(saveButton().disabled).toBe(true);
    expect(exportButton().disabled).toBe(true);
    answers.at(-1)?.();
    await waitFor(() => expect(saveButton().disabled).toBe(false));
    expect(exportButton().disabled).toBe(false);
  });

  it('Enter in the name does not save a setup edited a moment ago, and saves it once it has been read back', async () => {
    const service = fakeService(vi);
    await renderPage(service, download());
    await readyToSave('Mine');

    editWithoutLettingThePageCatchUp('Slippage (percent)', '0.5');
    pressEnterInTheName();

    expect(service.save).not.toHaveBeenCalled();
    await waitFor(() => {
      pressEnterInTheName();
      expect(service.save).toHaveBeenCalledTimes(1);
    });
    expect(service.save).toHaveBeenCalledWith('Mine', expect.objectContaining({ slippage_percent: '0.5' }));
  });

  it('Export does not hand over a setup edited a moment ago, and hands over the setup as the server read it back', async () => {
    const files = download();
    const service = fakeService(vi);
    await renderPage(service, files);
    await readyToSave('Mine');

    editWithoutLettingThePageCatchUp('Slippage (percent)', '0.5');
    exportButton().click();

    expect(files.save).not.toHaveBeenCalled();
    await waitFor(() => {
      exportButton().click();
      expect(files.save).toHaveBeenCalledTimes(1);
    });
    const [blob] = files.save.mock.calls[0] ?? [];
    const answer = await service.describe.mock.results.at(-1)?.value;
    expect(blob === undefined ? undefined : await readText(blob)).toBe(`${JSON.stringify(answer?.setup, null, 2)}\n`);
    expect(answer?.setup).toMatchObject({ slippage_percent: '0.5' });
  });
});

describe('saving', () => {
  it('goes once, however often Save is pressed and Enter is hit while it is under way', async () => {
    let finish: () => void = () => undefined;
    const service = fakeService(vi, {
      save: vi.fn(
        (name: string) =>
          new Promise<SavedSetup>((resolve) => {
            finish = () => resolve(savedAs(name));
          }),
      ),
    });
    await renderPage(service, download());
    await readyToSave('Mine');

    fireEvent.click(saveButton());
    fireEvent.click(saveButton());
    fireEvent.keyDown(nameField(), { key: 'Enter' });
    pressEnterInTheName();

    expect(service.save).toHaveBeenCalledTimes(1);
    expect(saveButton().disabled).toBe(true);
    finish();
    await screen.findByText(/Saved “Mine”/);
    expect(service.save).toHaveBeenCalledTimes(1);
  });

  it('is done by Enter in the name field, with the name and the setup on screen', async () => {
    const service = fakeService(vi);
    await renderPage(service, download());
    await readyToSave('My straddle');

    fireEvent.keyDown(nameField(), { key: 'Enter' });

    await waitFor(() => expect(service.save).toHaveBeenCalledWith('My straddle', readBackOf(exampleBody())));
    expect(service.save).toHaveBeenCalledTimes(1);
  });

  it('is not done by Enter when Save is not available', async () => {
    const service = fakeService(vi);
    await renderPage(service, download());
    typeInto('Setup name', 'Mine');
    await waitFor(() => expect(service.describe).toHaveBeenCalled());

    fireEvent.keyDown(nameField(), { key: 'Enter' });

    expect(saveButton().disabled).toBe(true);
    expect(service.save).not.toHaveBeenCalled();
  });
});
