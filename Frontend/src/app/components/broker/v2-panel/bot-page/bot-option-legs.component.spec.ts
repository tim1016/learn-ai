import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { BotOptionLegsComponent } from './bot-option-legs.component';

describe('BotOptionLegsComponent', () => {
  it('shows the held call and put, their actual quantities and recorded ratios', async () => {
    await render(BotOptionLegsComponent, { inputs: { traded: {
      kind: 'option', symbol: 'SPY', label: 'Custom',
      legs: [
        { leg_id: 'call', symbol: 'SPY261023C00585000', contract: { root: 'SPY', expiry_ms: 1_792_785_600_000, right: 'call', strike_thousandths: 585000 }, side: 'long', ratio: 2, multiplier: 100, quantity: 4 },
        { leg_id: 'put', symbol: 'SPY261023P00580000', contract: { root: 'SPY', expiry_ms: 1_792_785_600_000, right: 'put', strike_thousandths: 580000 }, side: 'short', ratio: 1, multiplier: 100, quantity: 2 },
      ],
    } } });
    expect(screen.getByRole('heading', { name: 'Held option legs' })).toBeTruthy();
    expect(screen.getByRole('button', { name: /SPY.*585.*Call/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /SPY.*580.*Put/ })).toBeTruthy();
    expect(screen.getByText('Long · 4 contracts')).toBeTruthy();
    expect(screen.getByText('Short · 2 contracts')).toBeTruthy();
    expect(screen.getByText(/call · Ratio 2 · Multiplier 100/)).toBeTruthy();
  });

  it('explains that there is no selected holding before entry', async () => {
    const notice = 'No option contracts held. Contracts are selected when an entry is accepted.';
    await render(BotOptionLegsComponent, { inputs: { traded: { kind: 'option', symbol: 'SPY', legs: [], legs_notice: notice } } });
    expect(screen.getByText(notice)).toBeTruthy();
  });
});
