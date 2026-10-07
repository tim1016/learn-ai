import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { OptionContractComponent, optionContractName, type OptionContract } from './option-contract.component';

// 2026-10-09 16:00 America/New_York = 2026-10-09 20:00 UTC.
const OCT_9_2026_MS = Date.UTC(2026, 9, 9, 20, 0, 0);

const CALL: OptionContract = {
  occ_symbol: 'SPY261009C00585000',
  underlying: 'SPY',
  expiry_ms: OCT_9_2026_MS,
  right: 'call',
  strike: '585',
};

/** The contract's name as it reads on screen: the underlying, then the rest. */
async function nameShownFor(contract: OptionContract): Promise<string> {
  const { container } = await render(OptionContractComponent, { inputs: { contract } });
  return (container.querySelector('.option-contract__name')?.textContent ?? '').replace(/\s+/g, ' ').trim();
}

describe('OptionContractComponent', () => {
  it('names a contract as a trader says it, with the year', async () => {
    expect(await nameShownFor(CALL)).toBe("SPY Oct 9 '26 585 Call");
  });

  it('draws the underlying with the shared symbol renderer, and no logo', async () => {
    const { container } = await render(OptionContractComponent, { inputs: { contract: CALL } });

    const underlying = container.querySelector('app-asset-identity');
    expect(underlying?.textContent?.trim()).toBe('SPY');
    expect(underlying?.querySelector('img')).toBeNull();
    expect(screen.getByText("Oct 9 '26 585 Call")).toBeTruthy();
  });

  it('says a put as a word', async () => {
    expect(await nameShownFor({ ...CALL, occ_symbol: 'SPY261009P00585000', right: 'put' })).toBe("SPY Oct 9 '26 585 Put");
  });

  it.each([
    ['587.5', "SPY Oct 9 '26 587.5 Call"],
    ['587.50', "SPY Oct 9 '26 587.5 Call"],
    ['585.000', "SPY Oct 9 '26 585 Call"],
    ['600', "SPY Oct 9 '26 600 Call"],
    [587.5, "SPY Oct 9 '26 587.5 Call"],
  ])('writes a strike of %s with no zeros it does not need', async (strike, name) => {
    expect(await nameShownFor({ ...CALL, strike })).toBe(name);
  });

  // A real expiry is 16:00 New York time. The two ends of the New York day
  // are used here because they are where a viewer's own zone would show
  // another date: the first is still October 8 in Chicago, the second is
  // already October 10 in UTC.
  it.each([
    ['the first minute of the New York day', Date.UTC(2026, 9, 9, 4, 0, 0)],
    ['the last minute of the New York day', Date.UTC(2026, 9, 10, 3, 59, 0)],
  ])('reads the expiry date in New York, never in the viewer zone: %s', async (_when, expiryMs) => {
    expect(await nameShownFor({ ...CALL, expiry_ms: expiryMs })).toBe("SPY Oct 9 '26 585 Call");
  });

  it('says the same name in words where no component can sit', () => {
    expect(optionContractName(CALL)).toBe("SPY Oct 9 '26 585 Call");
  });

  it('keeps the raw contract code one click away, behind a named control', async () => {
    await render(OptionContractComponent, { inputs: { contract: CALL } });

    expect(screen.queryByText('SPY261009C00585000')).toBeNull();
    const show = screen.getByRole('button', { name: "Show contract code for SPY Oct 9 '26 585 Call" });
    expect(show.getAttribute('aria-expanded')).toBe('false');

    await userEvent.click(show);

    const code = screen.getByText('SPY261009C00585000');
    const hide = screen.getByRole('button', { name: "Hide contract code for SPY Oct 9 '26 585 Call" });
    expect(hide.getAttribute('aria-expanded')).toBe('true');
    expect(hide.getAttribute('aria-controls')).toBe(code.id);

    await userEvent.click(hide);

    expect(screen.queryByText('SPY261009C00585000')).toBeNull();
  });
});
