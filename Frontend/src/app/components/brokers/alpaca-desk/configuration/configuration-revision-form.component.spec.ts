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

  it('shows a paper endpoint only the two extended-hours offsets, and says what they do', async () => {
    await render(HostComponent);

    const offsets = screen.getByRole('group', { name: 'Extended-hours offsets' });
    expect(screen.getAllByRole('spinbutton')).toHaveLength(2);
    expect(
      within(offsets).getByRole('spinbutton', { name: 'Extended-hours entry offset (bps)' }),
    ).toBeTruthy();
    expect(
      within(offsets).getByRole('spinbutton', { name: /^Extended-hours exit offset \(bps\)/ }),
    ).toBeTruthy();
    expect(screen.queryByRole('spinbutton', { name: 'Daily loss fraction' })).toBeNull();
    const note = within(offsets).getByText(/an exit the\s+exit offset/);
    expect(note.textContent).toMatch(/a short cover goes\s+the exit offset above it/);
    expect(note.textContent).toMatch(/will not Start until both are\s+set/);
    expect(note.textContent).toMatch(/will not Resume while it holds no position/);
    expect(note.textContent).toMatch(/a run still holding a position\s+always resumes/);
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

  it('reveals the six envelope fields for a live endpoint, and states that Live never arms', async () => {
    const rendered = await render(HostComponent);
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), 'live');
    await rendered.fixture.whenStable();

    expect(screen.getByRole('group', { name: 'Live risk envelope' })).toBeTruthy();
    expect(screen.getAllByRole('spinbutton')).toHaveLength(6);
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
