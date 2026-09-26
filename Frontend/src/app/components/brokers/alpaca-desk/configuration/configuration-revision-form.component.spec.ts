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

  it('offers every allowlisted slot and says which has no credentials injected', async () => {
    await render(HostComponent);

    expect(screen.getByRole('option', { name: 'Default credentials' })).toBeTruthy();
    expect(
      screen.getByRole('option', { name: 'Live credentials — no credentials injected' }),
    ).toBeTruthy();
  });

  it('says an unavailable slot is a host deployment change, not a page action', async () => {
    const rendered = await render(HostComponent);
    rendered.fixture.componentInstance.draft.update((draft) => ({
      ...draft,
      credential_slot: 'live',
    }));
    await rendered.fixture.whenStable();

    expect(screen.getByText(/No credential pair is injected for this slot/)).toBeTruthy();
    expect(screen.getByText(/this page cannot make it/)).toBeTruthy();
  });

  it('shows paper entry settings and exit defaults, and explains their ownership', async () => {
    await render(HostComponent);

    const offsets = screen.getByRole('group', { name: 'Extended-hours offsets' });
    expect(screen.getAllByRole('spinbutton')).toHaveLength(4);
    expect(
      within(offsets).getByRole('spinbutton', { name: 'Extended-hours entry offset (bps)' }),
    ).toBeTruthy();
    expect(
      within(offsets).getByRole('spinbutton', { name: /^Extended-hours exit offset \(bps\)/ }),
    ).toBeTruthy();
    expect(screen.queryByRole('spinbutton', { name: 'Daily loss fraction' })).toBeNull();
    const note = within(offsets).getByText(/The entry offset sets/);
    expect(note.textContent).toMatch(/each bot keeps the exit terms chosen when it was deployed/);
    expect(note.textContent).toMatch(/a sell below the bid or a cover above the ask/);
    expect(note.textContent).toMatch(/Start requires the bot's exit terms/);
    expect(note.textContent).toMatch(/must be flattened before Resume/);
    expect(screen.queryByText(/It does not arm live trading/)).toBeNull();
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

  it('reveals the live envelope and exit defaults, and explains where to arm', async () => {
    const rendered = await render(HostComponent);
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), 'live');
    await rendered.fixture.whenStable();

    expect(screen.getByRole('group', { name: 'Live risk envelope' })).toBeTruthy();
    expect(screen.getAllByRole('spinbutton')).toHaveLength(8);
    expect(screen.getByRole('spinbutton', { name: 'Daily loss fraction' })).toBeTruthy();
    expect(screen.getByRole('spinbutton', { name: 'Sessions one arming covers' })).toBeTruthy();
    expect(screen.getByText(/It does not arm live trading/)).toBeTruthy();
    expect(screen.getByText(/This page cannot arm anything/)).toBeTruthy();
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
