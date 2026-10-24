import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { SetupRefusedError } from '../../../components/options-backtest/options-backtest.service';
import type { SavedSetup } from '../../../components/options-backtest/options-backtest.types';
import { formatClockText } from '../../../components/options-backtest/session-time';
import { draftFromSetup } from '../../../components/options-backtest/setup-draft';
import { generatedSetupName } from '../../../components/options-backtest/setup-name';
import { fakeService } from '../../../components/options-backtest/testing/fake-service';
import { EXAMPLE_NO_REENTRY, EXAMPLE_ONE_REENTRY, exampleBody, noReentryExampleBody, readBackOf, servedExamples, servedSaved, SESSION_CLOCK } from '../../../components/options-backtest/testing/fixtures';
import { choose, flip, press, ruleSwitch, typeInto } from '../../../components/options-backtest/testing/page-actions';
import { openSetup, renderDefine } from '../../testing/render-workspace';

const { open_minute_et: OPEN, close_minute_et: CLOSE } = SESSION_CLOCK;

const saved = (name: string, savedAtMs: number): SavedSetup => servedSaved(name, { ...exampleBody(), underlying: 'AAPL' }, savedAtMs);
const bar = () => screen.getByRole('region', { name: 'Setup actions' });
const url = () => decodeURIComponent(TestBed.inject(Router).url);

describe('opening a setup by its address', () => {
  it('puts an example into the form, its times as clock times, and generates a setup name', async () => {
    await renderDefine(fakeService(vi), undefined, { setupName: EXAMPLE_ONE_REENTRY });

    await screen.findByRole('article', { name: 'Leg 2' });
    expect((screen.getByLabelText('Entry time') as HTMLInputElement).value).toBe(formatClockText(OPEN + 5));
    expect((screen.getByLabelText('Exit time') as HTMLInputElement).value).toBe(formatClockText(CLOSE - 46));
    expect((ruleSwitch('No re-entry after') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText('No re-entry after time') as HTMLInputElement).value).toBe(formatClockText(CLOSE - 60));
    expect((screen.getByRole('radio', { name: 'Complete' }) as HTMLInputElement).checked).toBe(true);
    expect((ruleSwitch('Leg 1 stop loss') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText('Leg 1 stop loss value') as HTMLInputElement).value).toBe('25');
    expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toContain('SPY, Buy');
  });

  it('opens the no-re-entry example with its re-entry rule and its cutoff off, and sends back exactly its JSON', async () => {
    const service = fakeService(vi);
    await renderDefine(service, undefined, { setupName: EXAMPLE_NO_REENTRY });

    await screen.findByRole('article', { name: 'Leg 2' });
    expect((ruleSwitch('No re-entry after') as HTMLInputElement).checked).toBe(false);
    for (const leg of [1, 2]) {
      expect((screen.getByRole('switch', { name: `Leg ${leg} re-entry on SL` }) as HTMLInputElement).checked).toBe(false);
      expect((screen.getByRole('switch', { name: `Leg ${leg} stop loss` }) as HTMLInputElement).checked).toBe(true);
      expect((screen.getByRole('switch', { name: `Leg ${leg} simple momentum` }) as HTMLInputElement).checked).toBe(true);
    }
    await waitFor(() => expect(service.describe).toHaveBeenLastCalledWith(noReentryExampleBody()));
  });

  it('puts a saved setup into the form under its own name, and titles the page with it', async () => {
    const service = fakeService(vi, { list: vi.fn(async () => [...servedExamples(), saved('Mine', 1_000)]) });
    await renderDefine(service, undefined, { setupName: 'Mine' });

    await waitFor(() => expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('Mine'));
    expect(screen.getByRole('heading', { level: 1, name: 'Mine' })).toBeTruthy();
    expect(url()).toBe('/options-setups/Mine/define');
  });

  it('opens another setup when the address changes to it, and does not reopen the one it already holds', async () => {
    const list = vi.fn(async () => [...servedExamples(), saved('Mine', 1_000), saved('Other', 900)]);
    const view = await renderDefine(fakeService(vi, { list }), undefined, { setupName: 'Mine' });
    await waitFor(() => expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('Mine'));
    typeInto('Entry time', '10:00');

    await openSetup(view, 'Other');

    await waitFor(() => expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('Other'));
    expect((screen.getByLabelText('Entry time') as HTMLInputElement).value).toBe(formatClockText(OPEN + 5));
  });
});

describe('the draft says whether it still matches what was saved', () => {
  it('matches a setup as opened, says it has changed once edited, and matches again once saved', async () => {
    const service = fakeService(vi, { list: vi.fn(async () => [...servedExamples(), saved('Mine', 1_000)]) });
    await renderDefine(service, undefined, { setupName: 'Mine' });
    await screen.findByText(/This draft matches “Mine” as last saved/);

    typeInto('Slippage (percent)', '0.9');

    await screen.findByText(/This draft has changed since it was saved as “Mine”/);
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Save setup' }));

    await screen.findByText(/This draft matches “Mine” as last saved/);
    expect(service.save).toHaveBeenCalledWith('Mine', expect.objectContaining({ slippage_percent: '0.9' }));
  });

  it('says a draft with no saved name is not saved, at its own address', async () => {
    await renderDefine(fakeService(vi));

    expect(screen.getByRole('heading', { level: 1, name: 'New options setup' })).toBeTruthy();
    expect(screen.getByText(/This draft has no saved name yet/)).toBeTruthy();
    expect(url()).toBe('/options-setups/_new/define');
  });

  it('says an edited example is not the shipped one, and that the example cannot be overwritten', async () => {
    await renderDefine(fakeService(vi), undefined, { setupName: EXAMPLE_ONE_REENTRY });
    await screen.findByText(/This draft is the shipped example/);

    typeInto('Slippage (percent)', '0.9');

    await screen.findByText(/changed from the shipped example .* which cannot be overwritten/);
  });
});

describe('a save that finishes after another setup was opened', () => {
  it('does not move the address to its name, and does not touch the form', async () => {
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
    const view = await renderDefine(service, undefined, { setupName: EXAMPLE_ONE_REENTRY });
    await screen.findByRole('article', { name: 'Leg 2' });
    typeInto('Setup name', 'Mine');
    const save = screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement;
    await waitFor(() => expect(save.disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(service.save).toHaveBeenCalledTimes(1));

    await openSetup(view, EXAMPLE_NO_REENTRY);
    await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: EXAMPLE_NO_REENTRY })).toBeTruthy());
    finishSave();

    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    expect(url()).toBe(`/options-setups/${EXAMPLE_NO_REENTRY}/define`);
    expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toContain('SPY, Buy');
    expect(within(bar()).queryByText(/Saved/)).toBeNull();
  });
});

describe('saving', () => {
  async function renderWithExampleOpen(service = fakeService(vi)) {
    await renderDefine(service, undefined, { setupName: EXAMPLE_ONE_REENTRY });
    await screen.findByRole('article', { name: 'Leg 2' });
    return service;
  }
  const saveButton = () => screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement;

  it('needs a name, and a setup the server has read back', async () => {
    await renderDefine(fakeService(vi));

    expect(saveButton().disabled).toBe(true);
    typeInto('Setup name', 'My straddle');
    expect(saveButton().disabled).toBe(true);
  });

  it('saves the setup on screen, as the server read it back, under the name typed, reads the list again, and moves the address to that name', async () => {
    const list = vi.fn(async () => servedExamples());
    const service = await renderWithExampleOpen(fakeService(vi, { list }));

    typeInto('Setup name', 'My straddle');
    await waitFor(() => expect(saveButton().disabled).toBe(false));
    fireEvent.click(saveButton());

    await waitFor(() => expect(service.save).toHaveBeenCalledWith('My straddle', readBackOf(exampleBody())));
    await within(bar()).findByText(/My straddle/);
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(url()).toBe('/options-setups/My straddle/define'));
    expect(screen.getByRole('heading', { level: 1, name: 'My straddle' })).toBeTruthy();
    // The same form is still there: the address moved, nothing was opened again.
    expect(screen.getByRole('article', { name: 'Leg 2' })).toBeTruthy();
  });

  it('adds a library entry under a new name and leaves the setup it came from as it was', async () => {
    const service = fakeService(vi, { list: vi.fn(async () => [...servedExamples(), saved('Mine', 1_000)]) });
    await renderDefine(service, undefined, { setupName: 'Mine' });
    await waitFor(() => expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('Mine'));

    typeInto('Slippage (percent)', '0.9');
    typeInto('Setup name', 'Mine, wider slippage');
    await waitFor(() => expect(saveButton().disabled).toBe(false));
    fireEvent.click(saveButton());

    await waitFor(() => expect(service.save).toHaveBeenCalledWith('Mine, wider slippage', expect.objectContaining({ slippage_percent: '0.9' })));
    expect(service.save).toHaveBeenCalledTimes(1);
    expect(service.remove).not.toHaveBeenCalled();
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
    expect(url()).toBe(`/options-setups/${EXAMPLE_ONE_REENTRY}/define`);
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

    expect(within(bar()).queryByText(/Saved “My straddle”/)).toBeNull();
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

describe('automatic setup naming', () => {
  it('refuses to overwrite a different saved setup under a colliding generated name', async () => {
    const label = generatedSetupName(draftFromSetup(noReentryExampleBody(), SESSION_CLOCK));
    const service = fakeService(vi, { list: vi.fn(async () => [...servedExamples(), servedSaved(label, exampleBody())]) });
    await renderDefine(service, undefined, { setupName: EXAMPLE_NO_REENTRY });
    await screen.findByText('A setup already uses this generated name. Choose a different name, or open the saved setup to edit it.');
    expect(screen.getByRole('button', { name: 'Save setup' })).toHaveProperty('disabled', true);
    expect(service.save).not.toHaveBeenCalled();
  });

  it('preserves a reopened saved name even when it matches the generated label', async () => {
    const setup = exampleBody();
    const label = generatedSetupName(draftFromSetup(setup, SESSION_CLOCK));
    await renderDefine(fakeService(vi, { list: vi.fn(async () => [servedSaved(label, setup)]) }), undefined, { setupName: label });
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Setup name' })).toHaveProperty('value', label));
    typeInto('Entry time', '10:00');
    expect(screen.getByRole('textbox', { name: 'Setup name' })).toHaveProperty('value', label);
    expect(screen.getByText('Custom name preserved.')).toBeTruthy();
  });

  it('updates a generated name with setup edits, preserves a custom edit and can return to automatic naming', async () => {
    await renderDefine(fakeService(vi), undefined, { setupName: EXAMPLE_NO_REENTRY });
    const name = await screen.findByRole('textbox', { name: 'Setup name' });
    await waitFor(() => expect(name).toHaveProperty('value', expect.stringContaining('SPY, Buy')));
    fireEvent.input(name, { target: { value: 'My custom research setup' } });
    typeInto('Entry time', '10:00');
    expect(name).toHaveProperty('value', 'My custom research setup');
    press('Use generated name');
    await waitFor(() => expect(name).toHaveProperty('value', expect.stringContaining('1000-')));
    typeInto('Entry time', '10:05');
    await waitFor(() => expect(name).toHaveProperty('value', expect.stringContaining('1005-')));
  });
});
