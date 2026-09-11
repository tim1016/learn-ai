import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerCredentialSlot } from '../../../../api/alpaca.types';
import { ConfigurationProfileCreateComponent } from './configuration-profile-create.component';

const SLOTS: readonly BrokerCredentialSlot[] = [
  { slot: 'default', label: 'Default credentials', available: true },
  { slot: 'live', label: 'Live credentials', available: false },
];

async function renderCreate(created = vi.fn()) {
  const rendered = await render(ConfigurationProfileCreateComponent, {
    inputs: { slots: SLOTS, busy: false },
    on: { created },
  });
  await userEvent.click(screen.getByRole('button', { name: 'New configuration profile' }));
  return rendered;
}

describe('ConfigurationProfileCreateComponent', () => {
  it('refuses to save until the profile has a name', async () => {
    await renderCreate();

    expect(screen.getByRole('button', { name: 'Save profile' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByText('Give the profile a name.')).toBeTruthy();
  });

  it('emits the name and the revision content, and never a credential', async () => {
    const created = vi.fn();
    await renderCreate(created);
    await userEvent.type(screen.getByLabelText('Profile name'), 'Paper — strategy testing');

    await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));

    expect(created).toHaveBeenCalledWith({
      displayName: 'Paper — strategy testing',
      content: { credential_slot: 'default', endpoint_mode: 'paper', live_envelope: null },
    });
  });

  it('keeps a half-typed profile when the slot list is re-read', async () => {
    // "Reload configuration" re-reads the slots, handing this component a new
    // array for the same allowlist. Re-seeding on that would throw away a
    // partly-entered live envelope.
    const rendered = await renderCreate();
    await userEvent.type(screen.getByLabelText('Profile name'), 'Paper — strategy testing');
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), 'live');
    await rendered.fixture.whenStable();

    rendered.fixture.componentRef.setInput('slots', [...SLOTS]);
    await rendered.fixture.whenStable();

    expect((screen.getByLabelText('Profile name') as HTMLInputElement).value).toBe(
      'Paper — strategy testing',
    );
    expect((screen.getByLabelText('Endpoint') as HTMLSelectElement).value).toBe('live');
  });

  it('refuses a live profile until every envelope value is supplied', async () => {
    await renderCreate();
    await userEvent.type(screen.getByLabelText('Profile name'), 'Live — real money');
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), 'live');

    expect(screen.getByRole('button', { name: 'Save profile' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByText('Daily loss fraction needs a number.')).toBeTruthy();
  });
});
