import { fireEvent, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { SetupLimits } from './options-backtest.types';
import { exampleBody, exampleLeg, SERVED_LIMITS, servedExamples, servedSaved } from './testing/fixtures';
import { addLeg, isDisabled, openExample, press, typeInto } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';

const addLegButton = () => screen.getByRole('button', { name: 'Add leg' });
const saveButton = () => screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement;

describe('what the page holds a setup to comes from the server', () => {
  it('refuses a contract count over the served limit, and takes the limit itself', async () => {
    const limits: SetupLimits = { ...SERVED_LIMITS, max_contracts_per_leg: 5 };
    await renderPage(fakeService(vi, { limits: vi.fn(async () => limits) }));

    typeInto('Contracts', '6');

    expect(screen.getByText('Enter a whole number of contracts, 1 to 5.')).toBeTruthy();
    expect(isDisabled(addLegButton())).toBe(true);
    typeInto('Contracts', '5');
    expect(isDisabled(addLegButton())).toBe(false);
  });

  it('moves the days and the steps with the served limits as well', async () => {
    const limits: SetupLimits = { ...SERVED_LIMITS, max_expiry_days: 10, max_strike_steps: 3 };
    await renderPage(fakeService(vi, { limits: vi.fn(async () => limits) }));

    fireEvent.click(screen.getByRole('radio', { name: 'At least N days' }));
    typeInto('Days (N)', '11');
    expect(screen.getByText('Enter a whole number of days, 0 to 10.')).toBeTruthy();
    typeInto('Days (N)', '10');
    fireEvent.click(screen.getByRole('radio', { name: 'Steps from the money' }));
    typeInto('Steps (+ above, - below)', '-4');

    expect(screen.getByText(/Enter a whole number of steps, -3 to 3, other than 0/)).toBeTruthy();
    typeInto('Steps (+ above, - below)', '-3');
    expect(isDisabled(addLegButton())).toBe(false);
  });

  it('holds the legs to the served count and says that count', async () => {
    const limits: SetupLimits = { ...SERVED_LIMITS, max_legs: 2 };
    await renderPage(fakeService(vi, { limits: vi.fn(async () => limits) }));

    addLeg();
    addLeg('Put');

    await screen.findByRole('article', { name: 'Leg 2' });
    expect(isDisabled(addLegButton())).toBe(true);
    expect(screen.getByText('The most legs a setup holds: 2. Delete one to add another.')).toBeTruthy();
    expect(screen.getByText('2 of 2')).toBeTruthy();
  });

  it('judges a setup’s name by the served pattern and shows the served sentence', async () => {
    const limits: SetupLimits = { ...SERVED_LIMITS, setup_name: { pattern: '^[a-z]+$', sentence: 'Use lower-case letters only.' } };
    await renderPage(fakeService(vi, { limits: vi.fn(async () => limits) }));
    addLeg();
    await screen.findByRole('article', { name: 'Leg 1' });

    typeInto('Setup name', 'Mine');

    expect(screen.getByText('Use lower-case letters only.')).toBeTruthy();
    expect(screen.getByLabelText('Setup name').getAttribute('aria-invalid')).toBe('true');
    expect(saveButton().disabled).toBe(true);
    typeInto('Setup name', 'mine');
    await waitFor(() => expect(saveButton().disabled).toBe(false));
  });
});

describe('while the limits cannot be had', () => {
  it('says plainly that they could not be read, lets the builder work, and leaves the judging to the server', async () => {
    // A setup that holds one leg more than the 8 the server serves, so that the page is not what stops it.
    const nine = servedSaved('Nine legs', { ...exampleBody(), legs: Array.from({ length: SERVED_LIMITS.max_legs + 1 }, (_, at) => ({ ...exampleLeg('call', false), leg_id: `saved-${at + 1}` })) });
    const service = fakeService(vi, {
      limits: vi.fn(async () => {
        throw new Error('the data service is down');
      }),
      list: vi.fn(async () => [...servedExamples(), nine]),
    });
    await renderPage(service);

    const notice = await screen.findByText(/The server's limits could not be read/);
    expect(notice.textContent).toContain('The server will judge every setup.');
    await openExample('Nine legs');
    await screen.findByRole('article', { name: 'Leg 9' });
    fireEvent.input(screen.getByRole('textbox', { name: 'Contracts' }), { target: { value: '5000' } });
    expect(isDisabled(addLegButton())).toBe(false);
    addLeg();
    await screen.findByRole('article', { name: 'Leg 10' });
    await waitFor(() => expect(service.describe.mock.lastCall?.[0]).toMatchObject({ legs: expect.any(Array) }));
    expect((service.describe.mock.lastCall?.[0] as { legs: unknown[] }).legs).toHaveLength(10);
    typeInto('Setup name', 'a/b');
    expect(screen.getByLabelText('Setup name').getAttribute('aria-invalid')).not.toBe('true');
  });

  it('reads the limits again on request and then holds the setup to them', async () => {
    let answer: SetupLimits | Error = new Error('down');
    const limits = vi.fn(async () => {
      if (answer instanceof Error) throw answer;
      return answer;
    });
    await renderPage(fakeService(vi, { limits }));
    await screen.findByText(/The server's limits could not be read/);

    answer = { ...SERVED_LIMITS, max_contracts_per_leg: 5 };
    press('Try again');

    await waitFor(() => expect(screen.queryByText(/The server's limits could not be read/)).toBeNull());
    typeInto('Contracts', '6');
    expect(screen.getByText('Enter a whole number of contracts, 1 to 5.')).toBeTruthy();
  });

  it('works, and says nothing, while the limits are still being read', async () => {
    const service = fakeService(vi, { limits: vi.fn(() => new Promise<SetupLimits>(() => undefined)) });
    await renderPage(service, undefined, { settled: false });

    typeInto('Contracts', '5000');
    addLeg();

    expect(screen.queryByText(/limits could not be read/)).toBeNull();
    await screen.findByRole('article', { name: 'Leg 1' });
  });
});
