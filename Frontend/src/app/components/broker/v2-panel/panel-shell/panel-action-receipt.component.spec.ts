import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import { type ActionReceiptView, PanelActionReceiptComponent } from './panel-action-receipt.component';

const NEXT_OPEN_MS = 1_753_862_400_000;

function refusal(overrides: Partial<ActionReceiptView> = {}): ActionReceiptView {
  return {
    actionId: 'execute_safe_flatten',
    outcome: 'failure',
    receiptId: null,
    recordedAtMs: 1_753_800_000_000,
    message: 'No trading session would be open when an order sent now reaches the broker.',
    remediation: 'Flatten again once the next session opens.',
    ...overrides,
  };
}

async function renderReceipt(receipt: ActionReceiptView) {
  return render(PanelActionReceiptComponent, { inputs: { receipt } });
}

function receiptText(): string {
  return screen.getByRole('alert', { name: 'Action outcome' }).textContent?.replace(/\s+/g, ' ').trim() ?? '';
}

describe('an action outcome', () => {
  it('names a refusal’s code in words and when the next session opens, in the viewer’s time', async () => {
    await renderReceipt(refusal({ reasonCode: 'NO_SESSION_OPEN', availableAtMs: NEXT_OPEN_MS }));

    const text = receiptText();
    expect(text).toContain('No Session Open');
    expect(text).not.toContain('NO_SESSION_OPEN');
    expect(text).toContain(`Next session opens ${formatTimestampDisplay(NEXT_OPEN_MS, { mode: 'local' })}`);
  });

  it('names the code of a simulated sale with no live price, and no next open it did not send', async () => {
    await renderReceipt(refusal({
      reasonCode: 'SIMULATED_RECOVERY_PRICE_UNAVAILABLE',
      message: 'There is no live IBKR price for this symbol right now.',
      remediation: 'Wait for this symbol’s IBKR quote while a trading session is open, then flatten again.',
    }));

    expect(receiptText()).toContain('Simulated Recovery Price Unavailable');
    expect(screen.queryByText(/Next session opens/)).toBeNull();
  });

  it('says a code once when the remediation already is its label', async () => {
    await renderReceipt(refusal({ reasonCode: 'stale_action_token', remediation: 'Stale Action Token' }));

    expect(receiptText().match(/Stale Action Token/g)).toHaveLength(1);
  });

  it('has no detectable accessibility violations', async () => {
    await renderReceipt(refusal({ reasonCode: 'NO_SESSION_OPEN', availableAtMs: NEXT_OPEN_MS }));

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
