import { render, screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { OptionContractComponent, optionContractName, type OptionContract } from './option-contract.component';

const CALL: OptionContract = {
  occ_symbol: 'SPY261009C00585000', underlying: 'SPY',
  expiry_ms: Date.UTC(2026, 9, 9, 20), right: 'call', strike: '585',
};

async function openDetails(): Promise<HTMLElement> {
  await userEvent.hover(screen.getByLabelText("SPY Oct 9 '26 585 Call"));
  return screen.findByRole('tooltip');
}

describe('OptionContractComponent', () => {
  it('keeps the underlying, right, strike and dated expiry visible in a compact identity', async () => {
    const { container } = await render(OptionContractComponent, { inputs: { contract: CALL } });
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText('Call')).toBeTruthy();
    expect(screen.getByText('585')).toBeTruthy();
    expect(screen.getByText("Oct 9 '26")).toBeTruthy();
    expect(container.querySelector('img')).toBeNull();
    expect(screen.queryByText(CALL.occ_symbol)).toBeNull();
  });

  it.each(['587.5', '587.50', '587.500', 587.5])('preserves a fractional strike (%s) without display zeros', async (strike) => {
    await render(OptionContractComponent, { inputs: { contract: { ...CALL, strike } } });
    expect(screen.getByText('587.5')).toBeTruthy();
  });

  it('distinguishes puts with their word as well as their color', async () => {
    await render(OptionContractComponent, { inputs: { contract: { ...CALL, right: 'put' } } });
    expect(screen.getByText('Put')).toBeTruthy();
  });

  it.each([Date.UTC(2026, 9, 9, 4), Date.UTC(2026, 9, 10, 3, 59)])('reads the expiry as its New York date (%s)', async (expiry_ms) => {
    await render(OptionContractComponent, { inputs: { contract: { ...CALL, expiry_ms } } });
    expect(screen.getByText("Oct 9 '26")).toBeTruthy();
  });

  it('reveals the broker code on hover without inventing missing market facts', async () => {
    await render(OptionContractComponent, { inputs: { contract: CALL } });
    const details = within(await openDetails());
    expect(details.getByText(CALL.occ_symbol)).toBeTruthy();
    expect(details.getByText(/Contract details only/)).toBeTruthy();
    expect(details.queryByText('Delta')).toBeNull();
    expect(screen.queryByText('ITM')).toBeNull();
  });

  it('keeps clicked contract details open after the pointer leaves the chip', async () => {
    await render(OptionContractComponent, { inputs: { contract: CALL } });
    const chip = screen.getByLabelText("SPY Oct 9 '26 585 Call");

    await userEvent.click(chip);
    await userEvent.unhover(chip);

    expect(await screen.findByText(CALL.occ_symbol)).toBeTruthy();
    await userEvent.click(document.body);
    await waitFor(() => expect(screen.queryByText(CALL.occ_symbol)).toBeNull());
  });

  it('shows only supplied finite facts, including zero values, with their snapshot time', async () => {
    await render(OptionContractComponent, { inputs: {
      contract: CALL,
      market: { bid: 0, ask: '4.95', delta: 0, impliedVolatility: 0.421, last: NaN, ibkrDelta: null,
        moneyness: 'ITM', asOfMs: Date.UTC(2026, 9, 9, 15), source: 'IBKR snapshot' },
    } });
    expect(screen.getByText('ITM')).toBeTruthy();
    const details = within(await openDetails());
    expect(details.getByText('$0.00')).toBeTruthy();
    expect(details.getByText('$4.95')).toBeTruthy();
    expect(details.getByText('0.00')).toBeTruthy();
    expect(details.getByText('42.1%')).toBeTruthy();
    expect(details.queryByText('Last')).toBeNull();
    expect(details.queryByText('IBKR delta')).toBeNull();
    expect(details.getByText('IBKR snapshot')).toBeTruthy();
    expect(details.getByText(/^As of/)).toBeTruthy();
  });

  it.each([
    ['call', 586, 'ITM'], ['call', 584, 'OTM'], ['put', 584, 'ITM'], ['put', 586, 'OTM'],
    ['call', 585, 'ATM'], ['put', 585, 'ATM'],
  ] as const)('classifies a %s against the supplied underlying price %s as %s', async (right, underlyingPrice, status) => {
    await render(OptionContractComponent, { inputs: { contract: { ...CALL, right }, market: { underlyingPrice } } });
    expect(screen.getByText(status)).toBeTruthy();
  });

  it.each([null, 0, -1, NaN, Infinity])('omits moneyness when the underlying price is unknown or unusable (%s)', async (underlyingPrice) => {
    await render(OptionContractComponent, { inputs: { contract: CALL, market: { underlyingPrice } } });
    expect(screen.queryByText(/^(ITM|ATM|OTM)$/)).toBeNull();
  });

  it('keeps the spoken contract name available to other UI', () => {
    expect(optionContractName(CALL)).toBe("SPY Oct 9 '26 585 Call");
  });
});
