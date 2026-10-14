import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { SetupRefusedError } from './options-backtest.service';
import type { SavedSetup } from './options-backtest.types';
import { formatClockText } from './session-time';
import { EXAMPLE_NO_REENTRY, EXAMPLE_ONE_REENTRY, exampleBody, noReentryExampleBody, readBackOf, servedExamples, servedSaved, SESSION_CLOCK } from './testing/fixtures';
import { choose, flip, press, typeInto } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';

const { open_minute_et: OPEN, close_minute_et: CLOSE } = SESSION_CLOCK;

const saved = (name: string, savedAtMs: number): SavedSetup => servedSaved(name, { ...exampleBody(), underlying: 'AAPL' }, savedAtMs);
const sideList = () => screen.getByRole('complementary', { name: 'Saved setups' });
const bar = () => screen.getByRole('region', { name: 'Setup actions' });
const openButton = (name: string) => within(sideList()).getByRole('button', { name: new RegExp(`^${name}`) });

describe('the saved setups list', () => {
  it('lists the examples first, marked as examples, then the saved setups as the server ordered them', async () => {
    const service = fakeService(vi, { list: vi.fn(async () => [...servedExamples(), saved('Newer one', 2_000), saved('Older one', 1_000)]) });
    await renderPage(service);

    await within(sideList()).findByText('Newer one');
    const rows = within(sideList()).getAllByRole('listitem');

    expect(rows.map((row) => row.querySelector('.name')?.textContent)).toEqual([EXAMPLE_NO_REENTRY, EXAMPLE_ONE_REENTRY, 'Newer one', 'Older one']);
    expect(rows.map((row) => within(row).queryByText('Example') !== null)).toEqual([true, true, false, false]);
  });

  it('has a delete control for a saved setup and none for an example', async () => {
    const service = fakeService(vi, { list: vi.fn(async () => [...servedExamples(), saved('Mine', 1_000)]) });
    await renderPage(service);

    await within(sideList()).findByText('Mine');

    expect(within(sideList()).getAllByRole('button', { name: /^Delete setup/ }).map((button) => button.getAttribute('aria-label'))).toEqual(['Delete setup Mine']);
  });

  it('lists a setup that can no longer be opened with its reason, and cannot load it', async () => {
    const broken: SavedSetup = { name: 'Old rules', example: false, saved_at_ms: 1_000, setup: null, cannot_open_reason: 'legs.0.contracts: Input should be at most 1000' };
    const service = fakeService(vi, { list: vi.fn(async () => [...servedExamples(), broken]) });
    await renderPage(service);

    const row = (await within(sideList()).findByText('Old rules')).closest('li');

    expect(row?.textContent).toContain('Input should be at most 1000');
    expect(within(row as HTMLElement).queryByRole('button', { name: /^Old rules/ })).toBeNull();
    expect(within(row as HTMLElement).getByRole('button', { name: 'Delete setup Old rules' })).toBeTruthy();
  });

  it('names the setup the form holds on the button that opens the list', async () => {
    await renderPage(fakeService(vi));
    const menuButton = () => screen.getByRole('button', { name: /^Saved setups/ });
    expect(menuButton().textContent).not.toContain(EXAMPLE_NO_REENTRY);

    fireEvent.click(await within(sideList()).findByRole('button', { name: new RegExp(`^${EXAMPLE_NO_REENTRY}`) }));

    await waitFor(() => expect(menuButton().textContent).toContain(EXAMPLE_NO_REENTRY));
  });

  it('says why when the list could not be read, and reads it again on request', async () => {
    let fail = true;
    const list = vi.fn(async () => {
      if (fail) throw new Error('database down');
      return servedExamples();
    });
    await renderPage(fakeService(vi, { list }));

    await within(sideList()).findByRole('alert');
    fail = false;
    press('Try again');

    await within(sideList()).findByText(EXAMPLE_NO_REENTRY);
  });
});

describe('opening a setup', () => {
  it('puts an example into the form, its times as clock times, and leaves the name empty', async () => {
    await renderPage(fakeService(vi));

    fireEvent.click(await within(sideList()).findByRole('button', { name: new RegExp(`^${EXAMPLE_ONE_REENTRY}`) }));

    await screen.findByRole('article', { name: 'Leg 2' });
    expect((screen.getByLabelText('Entry time') as HTMLInputElement).value).toBe(formatClockText(OPEN + 5));
    expect((screen.getByLabelText('Exit time') as HTMLInputElement).value).toBe(formatClockText(CLOSE - 46));
    expect((screen.getByRole('switch', { name: 'No re-entry after' }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText('No re-entry after time') as HTMLInputElement).value).toBe(formatClockText(CLOSE - 60));
    expect((screen.getByRole('radio', { name: 'Complete' }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole('switch', { name: 'Leg 1 stop loss' }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText('Leg 1 stop loss value') as HTMLInputElement).value).toBe('25');
    expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('');
  });

  it('opens the no-re-entry example with its re-entry rule and its cutoff off, and sends back exactly its JSON', async () => {
    const service = fakeService(vi);
    await renderPage(service);

    fireEvent.click(await within(sideList()).findByRole('button', { name: new RegExp(`^${EXAMPLE_NO_REENTRY}`) }));

    await screen.findByRole('article', { name: 'Leg 2' });
    expect((screen.getByRole('switch', { name: 'No re-entry after' }) as HTMLInputElement).checked).toBe(false);
    for (const leg of [1, 2]) {
      expect((screen.getByRole('switch', { name: `Leg ${leg} re-entry on SL` }) as HTMLInputElement).checked).toBe(false);
      expect((screen.getByRole('switch', { name: `Leg ${leg} stop loss` }) as HTMLInputElement).checked).toBe(true);
      expect((screen.getByRole('switch', { name: `Leg ${leg} simple momentum` }) as HTMLInputElement).checked).toBe(true);
    }
    await waitFor(() => expect(service.describe).toHaveBeenLastCalledWith(noReentryExampleBody()));
  });

  it('puts a saved setup into the form under its own name', async () => {
    const service = fakeService(vi, { list: vi.fn(async () => [...servedExamples(), saved('Mine', 1_000)]) });
    await renderPage(service);

    fireEvent.click(await within(sideList()).findByRole('button', { name: /^Mine/ }));

    await waitFor(() => expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('Mine'));
    expect(within(sideList()).getByRole('button', { name: /^Mine/ }).getAttribute('aria-current')).toBe('true');
  });
});

describe('deleting', () => {
  it('asks once, deletes the saved setup by name and reads the list again', async () => {
    const list = vi.fn(async () => [...servedExamples(), saved('Mine', 1_000)]);
    const service = fakeService(vi, { list });
    await renderPage(service);
    await within(sideList()).findByText('Mine');

    press('Delete setup Mine');
    expect(service.remove).not.toHaveBeenCalled();
    press('Delete');

    await waitFor(() => expect(service.remove).toHaveBeenCalledWith('Mine'));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  });

  it('shows the server’s reason when it refuses, and still reads the list again', async () => {
    const list = vi.fn(async () => [...servedExamples(), saved('Gone', 1_000)]);
    const service = fakeService(vi, {
      list,
      remove: vi.fn(async () => {
        throw new SetupRefusedError('OPTIONS_BACKTEST_SETUP_NOT_FOUND', "No saved setup is named 'Gone'.", 404);
      }),
    });
    await renderPage(service);
    await within(sideList()).findByText('Gone');

    press('Delete setup Gone');
    press('Delete');

    await within(sideList()).findByText("No saved setup is named 'Gone'.");
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  });
});

describe('deleting the setup on screen', () => {
  it('stops the bar saying it was saved, and clears the name and the highlight', async () => {
    // The list still lists "Mine" after the delete, as if another tab saved it again: only the highlight can say the form no longer holds it.
    const list = vi.fn(async () => [...servedExamples(), saved('Mine', 1_000)]);
    const service = fakeService(vi, { list });
    await renderPage(service);
    fireEvent.click(await within(sideList()).findByRole('button', { name: new RegExp(`^${EXAMPLE_ONE_REENTRY}`) }));
    await screen.findByRole('article', { name: 'Leg 2' });
    typeInto('Setup name', 'Mine');
    const save = screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement;
    await waitFor(() => expect(save.disabled).toBe(false));
    fireEvent.click(save);
    await within(bar()).findByText(/Saved “Mine”/);
    await waitFor(() => expect(openButton('Mine').getAttribute('aria-current')).toBe('true'));

    press('Delete setup Mine');
    press('Delete');

    await waitFor(() => expect(service.remove).toHaveBeenCalledWith('Mine'));
    await waitFor(() => expect(within(bar()).queryByText(/Saved/)).toBeNull());
    expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('');
    expect(openButton('Mine').getAttribute('aria-current')).toBeNull();

    // Typing the same name again must not bring the old "Saved" back: the setup is no longer saved.
    typeInto('Setup name', 'Mine');
    expect(within(bar()).queryByText(/Saved/)).toBeNull();
  });

  it('leaves a name the owner has since typed over, and the row of the setup that is on screen', async () => {
    const service = fakeService(vi, { list: vi.fn(async () => [...servedExamples(), saved('Mine', 1_000), saved('Other', 900)]) });
    await renderPage(service);
    fireEvent.click(await within(sideList()).findByRole('button', { name: /^Mine/ }));
    await waitFor(() => expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('Mine'));
    typeInto('Setup name', 'A new name');

    press('Delete setup Other');
    press('Delete');

    await waitFor(() => expect(service.remove).toHaveBeenCalledWith('Other'));
    expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('A new name');
    expect(openButton('Mine').getAttribute('aria-current')).toBe('true');
  });
});

describe('a save that finishes after another setup was opened', () => {
  it('does not re-select its own row, and does not touch the form', async () => {
    let finishSave: () => void = () => undefined;
    const mine = saved('Mine', 2_000);
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
    await renderPage(service);
    fireEvent.click(await within(sideList()).findByRole('button', { name: new RegExp(`^${EXAMPLE_ONE_REENTRY}`) }));
    await screen.findByRole('article', { name: 'Leg 2' });
    typeInto('Setup name', 'Mine');
    const save = screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement;
    await waitFor(() => expect(save.disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(service.save).toHaveBeenCalledTimes(1));

    fireEvent.click(within(sideList()).getByRole('button', { name: new RegExp(`^${EXAMPLE_NO_REENTRY}`) }));
    await waitFor(() => expect(openButton(EXAMPLE_NO_REENTRY).getAttribute('aria-current')).toBe('true'));
    finishSave();

    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    expect(openButton('Mine').getAttribute('aria-current')).toBeNull();
    expect(openButton(EXAMPLE_NO_REENTRY).getAttribute('aria-current')).toBe('true');
    expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('');
    expect(within(bar()).queryByText(/Saved/)).toBeNull();
  });
});

describe('saving', () => {
  async function renderWithExampleOpen(service = fakeService(vi)) {
    await renderPage(service);
    fireEvent.click(await within(sideList()).findByRole('button', { name: new RegExp(`^${EXAMPLE_ONE_REENTRY}`) }));
    await screen.findByRole('article', { name: 'Leg 2' });
    return service;
  }
  const saveButton = () => screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement;

  it('needs a name, and a setup the server has read back', async () => {
    await renderPage(fakeService(vi));

    expect(saveButton().disabled).toBe(true);
    typeInto('Setup name', 'My straddle');
    expect(saveButton().disabled).toBe(true);
  });

  it('saves the setup on screen, as the server read it back, under the name typed, and reads the list again', async () => {
    const list = vi.fn(async () => servedExamples());
    const service = await renderWithExampleOpen(fakeService(vi, { list }));

    typeInto('Setup name', 'My straddle');
    await waitFor(() => expect(saveButton().disabled).toBe(false));
    fireEvent.click(saveButton());

    await waitFor(() => expect(service.save).toHaveBeenCalledWith('My straddle', readBackOf(exampleBody())));
    await within(bar()).findByText(/My straddle/);
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  });

  it('shows the server’s reason when the name is an example’s', async () => {
    const service = await renderWithExampleOpen(
      fakeService(vi, {
        save: vi.fn(async () => {
          throw new SetupRefusedError('OPTIONS_BACKTEST_SETUP_IS_AN_EXAMPLE', `'${EXAMPLE_NO_REENTRY}' is one of the shipped examples, which cannot be overwritten.`, 409);
        }),
      }),
    );

    typeInto('Setup name', EXAMPLE_NO_REENTRY);
    await waitFor(() => expect(saveButton().disabled).toBe(false));
    fireEvent.click(saveButton());

    const reason = await screen.findByText(/shipped examples, which cannot be overwritten/);
    expect(reason.getAttribute('role')).toBe('alert');
    expect(service.save).toHaveBeenCalledTimes(1);
  });

  it('says the name rule at the name, and sends nothing, for a name the server would not take', async () => {
    const service = await renderWithExampleOpen();

    typeInto('Setup name', 'a/b');

    expect(saveButton().disabled).toBe(true);
    expect(screen.getByLabelText('Setup name').getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('alert').textContent).toMatch(/1 to 80 characters/);
    expect(service.save).not.toHaveBeenCalled();
  });

  it('stops saying “Saved” once the setup changes, because it no longer describes what is on screen', async () => {
    await renderWithExampleOpen();
    typeInto('Setup name', 'My straddle');
    await waitFor(() => expect(saveButton().disabled).toBe(false));
    fireEvent.click(saveButton());
    await within(bar()).findByText(/My straddle/);

    typeInto('Slippage (percent)', '0.9');

    expect(within(bar()).queryByText(/My straddle/)).toBeNull();
  });

  it('is available for an edited example only under a new name, and sends the edit', async () => {
    const service = await renderWithExampleOpen();
    choose('Partial');
    flip('Leg 1 stop loss');

    typeInto('Setup name', 'Edited');
    await waitFor(() => expect(saveButton().disabled).toBe(false));
    fireEvent.click(saveButton());

    await waitFor(() => expect(service.save).toHaveBeenCalledWith('Edited', expect.objectContaining({ square_off: 'partial' })));
    const sent = service.save.mock.lastCall?.[1];
    // The server reads a rule that is off back as an explicit null.
    expect(sent?.legs[0]?.stop_loss).toBeNull();
  });
});
