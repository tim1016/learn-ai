import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { LegalNoticesPageComponent } from './legal-notices-page.component';

describe('LegalNoticesPageComponent', () => {
  it('publishes the TradingView notice and attribution link', async () => {
    await render(LegalNoticesPageComponent);

    expect(
      screen.getByRole('heading', { name: 'Open-source notices', level: 1 }),
    ).toBeTruthy();
    expect(screen.getByText('TradingView Lightweight Charts™')).toBeTruthy();
    expect(screen.getByText(/Copyright \(с\) 2025 TradingView, Inc\./)).toBeTruthy();
    expect(
      screen.getByRole('link', { name: 'https://www.tradingview.com/' }).getAttribute('href'),
    ).toBe('https://www.tradingview.com/');
  });
});
