import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { AlpacaPortfolioReconciliationProofComponent } from './alpaca-portfolio-reconciliation-proof.component';

describe('AlpacaPortfolioReconciliationProofComponent', () => {
  it('renders a single heading for the proof section (#2183)', async () => {
    await render(AlpacaPortfolioReconciliationProofComponent, {
      inputs: { proof: undefined, unavailable: false },
    });

    // #2183: "Portfolio equity check" carries the eyebrow look itself now;
    // the separate "Account verification" label above it is retired.
    expect(screen.getByRole('heading', { name: 'Portfolio equity check' })).toBeTruthy();
    expect(screen.queryByText('Account verification')).toBeNull();
  });

  it('has no detectable accessibility violations', async () => {
    await render(AlpacaPortfolioReconciliationProofComponent, {
      inputs: { proof: undefined, unavailable: false },
    });

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
