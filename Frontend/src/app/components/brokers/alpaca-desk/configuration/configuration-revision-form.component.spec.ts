import { Component, signal } from '@angular/core';
import { form } from '@angular/forms/signals';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { BrokerCredentialSlot } from '../../../../api/alpaca.types';
import { ConfigurationRevisionFormComponent } from './configuration-revision-form.component';
import { emptyDraft } from './configuration-revision-draft';

const SLOTS: readonly BrokerCredentialSlot[] = [
  { slot: 'default', label: 'Default credentials', available: true },
  { slot: 'live', label: 'Live credentials', available: false },
];

@Component({
  imports: [ConfigurationRevisionFormComponent],
  template: `<app-configuration-revision-form
    [draftForm]="draftForm"
    [slots]="slots"
    [problems]="problems()"
  />`,
})
class HostComponent {
  readonly slots = SLOTS;
  readonly draft = signal(emptyDraft('default'));
  readonly draftForm = form(this.draft);
  readonly problems = signal<readonly string[]>([]);
}

describe('ConfigurationRevisionFormComponent', () => {
  it.each(['paper', 'live'] as const)('accepts backend-valid fractional values on a %s revision', async (endpoint) => {
    const rendered = await render(HostComponent);
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), endpoint);
    await rendered.fixture.whenStable();

    const values = [
      ['Extended-hours entry offset (bps)', '7.25'],
      [/^Extended-hours exit offset \(bps\)/, '8.125'],
      ...(endpoint === 'live' ? [['Daily loss fraction', '0.00025'], ['Daily loss cap (USD)', '12.34']] : []),
    ] as const;
    for (const [name, value] of values) {
      const field = screen.getByRole('spinbutton', { name });
      if (!(field instanceof HTMLInputElement)) throw new Error('Expected a numeric input');
      await userEvent.type(field, value);
      await rendered.fixture.whenStable();
      expect(field.value).toBe(value);
      expect(field.checkValidity()).toBe(true);
    }
  });

  it('offers every allowlisted slot, and shows paper entry settings and exit defaults with their ownership', async () => {
    await render(HostComponent);

    expect.soft(
      screen.queryByRole('spinbutton', { name: 'Daily loss fraction' }),
      'paper settings: no daily loss fraction',
    ).toBeNull();
    expect.soft(screen.queryByText(/Every Live deployment/), 'paper settings: no Live deployment note').toBeNull();
    expect.soft(
      screen.getByRole('option', { name: 'Default credentials' }),
      'offers every allowlisted slot: Default credentials',
    ).toBeTruthy();
    expect.soft(
      screen.getByRole('option', { name: 'Live credentials — no credentials injected' }),
      'says which slot has no credentials injected',
    ).toBeTruthy();

    const offsets = screen.getByRole('group', { name: 'Extended-hours offsets' });
    expect.soft(screen.getAllByRole('spinbutton'), 'paper settings: four number fields').toHaveLength(4);
    expect.soft(
      within(offsets).getByRole('spinbutton', { name: 'Extended-hours entry offset (bps)' }),
      'paper settings: entry offset field',
    ).toBeTruthy();
    expect.soft(
      within(offsets).getByRole('spinbutton', { name: /^Extended-hours exit offset \(bps\)/ }),
      'paper settings: exit offset field',
    ).toBeTruthy();
    const note = within(offsets).getByText(/The entry offset sets/);
    expect.soft(note.textContent, 'ownership note: bots keep their deployed exit terms')
      .toMatch(/each bot keeps the exit terms chosen when it was deployed/);
    expect.soft(note.textContent, 'ownership note: sell below the bid or cover above the ask')
      .toMatch(/a sell below the bid or a cover above the ask/);
    expect.soft(note.textContent, 'ownership note: stopped positions keep their recovery actions')
      .toMatch(/Stopped\s+positions retain their existing recovery actions/);
    expect.soft(note.textContent, 'ownership note: every fresh Deploy reviews its exit terms')
      .toMatch(/Every fresh Deploy reviews its own exit terms/);
  });

  it('keeps typed offsets when the endpoint switches between paper and live', async () => {
    const rendered = await render(HostComponent);
    await userEvent.type(
      screen.getByRole('spinbutton', { name: 'Extended-hours entry offset (bps)' }),
      '12.5',
    );
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), 'live');
    await rendered.fixture.whenStable();

    expect(
      (screen.getByRole('spinbutton', { name: 'Extended-hours entry offset (bps)' }) as HTMLInputElement)
        .value,
    ).toBe('12.5');
    expect(rendered.fixture.componentInstance.draft().xh_entry_bps).toBe(12.5);
  });

  it('reveals legacy startup defaults and directs active risk edits to Daily loss limit', async () => {
    const rendered = await render(HostComponent);
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), 'live');
    await rendered.fixture.whenStable();

    expect(screen.getByRole('group', { name: 'Live risk envelope' })).toBeTruthy();
    expect(screen.getAllByRole('spinbutton')).toHaveLength(6);
    expect(screen.getByRole('spinbutton', { name: 'Daily loss fraction' })).toBeTruthy();
    expect(screen.queryByRole('spinbutton', { name: 'Sessions one arming covers' })).toBeNull();
    expect(screen.queryByRole('spinbutton', { name: 'Shadow sessions required' })).toBeNull();
    expect(screen.getByText(/Every Live deployment/)).toBeTruthy();
    expect(screen.getByText(/Use Daily loss limit at the top of Settings/)).toBeTruthy();
  });

  it('starts every live value blank rather than proposing a limit', async () => {
    const rendered = await render(HostComponent);
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), 'live');
    await rendered.fixture.whenStable();

    for (const field of screen.getAllByRole('spinbutton')) {
      expect((field as HTMLInputElement).value).toBe('');
    }
  });

  it('lists the problems it was handed rather than deriving its own', async () => {
    const rendered = await render(HostComponent);
    rendered.fixture.componentInstance.problems.set(['Daily loss cap (USD) needs a number.']);
    await rendered.fixture.whenStable();

    expect(screen.getByText('Daily loss cap (USD) needs a number.')).toBeTruthy();
  });
});
