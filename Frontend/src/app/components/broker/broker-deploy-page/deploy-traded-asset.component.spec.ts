import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { DeployTradedAssetComponent } from './deploy-traded-asset.component';

const SUMMARY = 'Trades SPY shares, the symbol it listens to';
const WARNING = {
  headline: 'Tested trading SPY. Untested trading QQQ.',
  detail: 'This strategy has not been tested buying QQQ.',
  acceptance_label: null,
};

async function renderLine(inputs: Record<string, unknown> = {}) {
  const tradedSymbolChange = vi.fn();
  await render(DeployTradedAssetComponent, {
    inputs: { summary: SUMMARY, ...inputs },
    on: { tradedSymbolChange },
    providers: fakePickerWorld().providers,
  });
  return { tradedSymbolChange, user: userEvent.setup() };
}

describe('DeployTradedAssetComponent', () => {
  it("is closed by default and says what the bot trades in the backend's own line", async () => {
    await renderLine();

    const toggle = screen.getByRole('button', { name: SUMMARY });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('radiogroup')).toBeNull();
  });

  it('offers the choice once opened, with the same symbol chosen', async () => {
    const { user } = await renderLine();

    await user.click(screen.getByRole('button', { name: SUMMARY }));

    const choices = screen.getByRole('radiogroup', { name: 'What it trades' });
    expect(choices).toBeTruthy();
    expect((screen.getByRole('radio', { name: 'Same as signal' }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole('radio', { name: 'Another stock or ETF' }) as HTMLInputElement).checked).toBe(false);
    expect(screen.queryByRole('note')).toBeNull();
  });

  it('opens on a bot that already trades something else, with its warning', async () => {
    await renderLine({ tradedSymbol: 'QQQ', warning: WARNING });

    expect((screen.getByRole('radio', { name: 'Another stock or ETF' }) as HTMLInputElement).checked).toBe(true);
    const warning = screen.getByRole('note');
    expect(warning.textContent).toContain('Tested trading SPY. Untested trading QQQ.');
    expect(warning.textContent).toContain('This strategy has not been tested buying QQQ.');
  });

  it('clears what was picked when the owner goes back to the same symbol', async () => {
    const { tradedSymbolChange, user } = await renderLine({ tradedSymbol: 'QQQ', warning: WARNING });

    await user.click(screen.getByRole('radio', { name: 'Same as signal' }));

    expect(tradedSymbolChange).toHaveBeenCalledWith(null);
  });

  it("says in the backend's words why a picked asset cannot be traded", async () => {
    await renderLine({ tradedSymbol: 'XYZ', refusal: 'Alpaca cannot trade XYZ. Choose another asset.' });

    expect(screen.getByRole('alert').textContent).toContain('Alpaca cannot trade XYZ. Choose another asset.');
  });
});
