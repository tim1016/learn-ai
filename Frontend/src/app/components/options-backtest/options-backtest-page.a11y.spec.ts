import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { addLeg, flip, inLeg, legCard, press, typeInto } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';

describe('the page for a screen reader', () => {
  it('has no accessibility violations with a setup open, its rules on, and a problem showing', async () => {
    await renderPage(fakeService(vi));
    fireEvent.click(await within(screen.getByRole('complementary', { name: 'Saved setups' })).findByRole('button', { name: /^SPY premium momentum, one re-entry/ }));
    await screen.findByRole('article', { name: 'Leg 2' });
    typeInto('Leg 1 stop loss value', '');
    expect(inLeg(1).getByRole('alert')).toBeTruthy();

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.html).join(' | ')}`)).toEqual([]);
  });

  it('names every switch with the words written beside it', async () => {
    await renderPage(fakeService(vi));
    addLeg();
    addLeg('Put');

    const switches = screen.getAllByRole<HTMLInputElement>('switch');

    expect(switches.length).toBeGreaterThan(6);
    for (const control of switches) {
      const written = control.closest('label')?.textContent?.trim().toLowerCase() ?? '';
      // A switch with no aria-label is named by the words beside it.
      const name = (control.getAttribute('aria-label') ?? written).toLowerCase();
      expect(written, `a switch named "${name}" has no words beside it`).not.toBe('');
      expect(name, `"${name}" does not contain what is written beside the switch, "${written}"`).toContain(written);
    }
  });
});

describe('where the keyboard goes when a leg changes', () => {
  async function renderWithLegs(count: number) {
    await renderPage(fakeService(vi));
    for (let leg = 0; leg < count; leg += 1) addLeg(leg % 2 === 0 ? 'Call' : 'Put');
    await screen.findByRole('article', { name: `Leg ${count}` });
  }

  it('goes to the delete control of the next leg when a leg in the middle is deleted', async () => {
    await renderWithLegs(3);

    press('Delete Leg 2');

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete Leg 2' })));
    expect(legCard(2).contains(document.activeElement)).toBe(true);
  });

  it('goes to the delete control of the leg before when the last leg is deleted', async () => {
    await renderWithLegs(3);

    press('Delete Leg 3');

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete Leg 2' })));
  });

  it('goes to Add leg when the only leg is deleted', async () => {
    await renderWithLegs(1);

    press('Delete Leg 1');

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Add leg' })));
  });

  it('goes to the first builder field instead, when Add leg cannot be pressed until something there is put right', async () => {
    await renderWithLegs(1);
    fireEvent.input(screen.getByRole('textbox', { name: 'Contracts' }), { target: { value: '5000' } });

    press('Delete Leg 1');

    await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText('Contracts')));
  });

  it('goes to the new leg when a leg is copied', async () => {
    await renderWithLegs(2);

    press('Copy Leg 1');

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Leg 2' })));
    expect(legCard(2).contains(document.activeElement)).toBe(true);
    expect(screen.getByRole('article', { name: 'Leg 3' })).toBeTruthy();
  });

  it('stays on Add leg when a leg is added', async () => {
    await renderWithLegs(1);
    const add = screen.getByRole('button', { name: 'Add leg' });
    add.focus();

    fireEvent.click(add);
    await screen.findByRole('article', { name: 'Leg 2' });

    expect(document.activeElement).toBe(add);
  });
});

describe('a problem is tied to its field', () => {
  const describedText = (field: HTMLElement) =>
    (field.getAttribute('aria-describedby') ?? '')
      .split(' ')
      .map((id) => document.getElementById(id)?.textContent ?? '')
      .join(' ');

  it('in the leg builder', async () => {
    await renderPage(fakeService(vi));
    const contracts = screen.getByLabelText('Contracts');
    expect(contracts.getAttribute('aria-describedby')).toBeNull();

    typeInto('Contracts', '5000');

    expect(describedText(contracts)).toMatch(/contracts/i);
    expect(document.getElementById(contracts.getAttribute('aria-describedby') ?? '')?.getAttribute('role')).toBe('alert');
  });

  it('in a leg’s stop loss, momentum and re-entry', async () => {
    await renderPage(fakeService(vi));
    addLeg();

    flip('Leg 1 stop loss');
    flip('Leg 1 simple momentum');
    flip('Leg 1 re-entry on SL');
    typeInto('Leg 1 re-entries', 'many');

    expect(describedText(screen.getByLabelText('Leg 1 stop loss value'))).toMatch(/number/i);
    expect(describedText(screen.getByLabelText('Leg 1 momentum value'))).toMatch(/number/i);
    expect(describedText(screen.getByLabelText('Leg 1 re-entries'))).toMatch(/whole number/i);
    expect(describedText(screen.getByRole('group', { name: 'Leg 1 stop loss unit' }))).toMatch(/number/i);
  });

  it('keeps the reason a re-entry on momentum is unavailable, beside its problem', async () => {
    await renderPage(fakeService(vi));
    addLeg();
    flip('Leg 1 re-entry on SL');
    typeInto('Leg 1 re-entries', 'many');

    const kind = screen.getByRole('group', { name: 'Leg 1 re-entry kind' });

    expect(describedText(kind)).toMatch(/momentum/i);
    expect(describedText(kind)).toMatch(/whole number/i);
  });
});
