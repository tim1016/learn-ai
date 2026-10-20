import { render } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { AlpacaPortfolioReconciliationProofComponent } from './alpaca-portfolio-reconciliation-proof.component';

describe('AlpacaPortfolioReconciliationProofComponent', () => {
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
