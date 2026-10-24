import { screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { SessionClock } from '../../../components/options-backtest/options-backtest.types';
import { EXAMPLE_NO_REENTRY, SESSION_CLOCK } from '../../../components/options-backtest/testing/fixtures';
import { press } from '../../../components/options-backtest/testing/page-actions';
import { fakeService } from '../../../components/options-backtest/testing/fake-service';
import { renderDefine } from '../../testing/render-workspace';

describe('before the session clock is read', () => {
  it('shows nothing that holds a time while it is being read', async () => {
    const pending = new Promise<SessionClock>(() => undefined);
    const service = fakeService(vi, { sessionClock: vi.fn(() => pending) });

    await renderDefine(service, undefined, { form: false, settled: false });

    expect(screen.queryByRole('heading', { name: 'Entry settings' })).toBeNull();
    expect(((await screen.findByRole('button', { name: 'Import setup' })) as HTMLButtonElement).disabled).toBe(true);
    expect(service.describe).not.toHaveBeenCalled();
  });

  it('says why the clock could not be read, offers to read it again, and then shows the form', async () => {
    let fail = true;
    const sessionClock = vi.fn(async () => {
      if (fail) throw new Error('data plane down');
      return SESSION_CLOCK;
    });

    await renderDefine(fakeService(vi, { sessionClock }), undefined, { form: false });

    await screen.findByText(/could not be read/i);
    expect(screen.queryByRole('heading', { name: 'Entry settings' })).toBeNull();
    fail = false;
    press('Try again');

    await screen.findByRole('heading', { name: 'Entry settings' });
    expect((screen.getByLabelText('Entry time') as HTMLInputElement).value).not.toBe('');
  });

  it('opens the saved setup its address names only once the clock is read, because its times need it', async () => {
    let release: (clock: SessionClock) => void = () => undefined;
    const service = fakeService(vi, { sessionClock: vi.fn(() => new Promise<SessionClock>((resolve) => (release = resolve))) });
    await renderDefine(service, undefined, { form: false, settled: false, setupName: EXAMPLE_NO_REENTRY });

    await screen.findByText('Reading the session times…');
    expect(screen.queryByRole('article', { name: 'Leg 1' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Entry settings' })).toBeNull();

    release(SESSION_CLOCK);

    await screen.findByRole('heading', { name: 'Entry settings' });
    await screen.findByRole('article', { name: 'Leg 1' });
    await waitFor(() => expect(service.describe).toHaveBeenCalled());
  });
});
