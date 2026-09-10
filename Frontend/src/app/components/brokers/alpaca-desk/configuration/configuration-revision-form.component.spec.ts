import { Component, signal } from '@angular/core';
import { form } from '@angular/forms/signals';
import { render, screen } from '@testing-library/angular';
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

  it('reveals the six envelope fields for a live endpoint, and states that Live never arms', async () => {
    const rendered = await render(HostComponent);
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), 'live');
    await rendered.fixture.whenStable();

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
