import { render } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import { of } from 'rxjs';

import { MarketDataService } from '../../../services/market-data.service';
import { OptionsLabChainComponent } from './options-lab-chain.component';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';

describe('OptionsLabChainComponent render', () => {
  it('renders the toolbar underlying with the shared asset identity', async () => {
    const { container } = await render(OptionsLabChainComponent, {
      providers: [
        ...fakePickerWorld().providers,
        {
        provide: MarketDataService,
        useValue: {
          getOptionsExpirations: () => of([]),
          getStockSnapshot: () => of({ success: false, snapshot: null }),
        },
      }],
    });

    // Scoped to the pill: the toolbar's symbol picker also renders the
    // current ticker through an asset identity.
    const identity = container.querySelector('.ticker-pill app-asset-identity');
    expect(identity).not.toBeNull();
    expect(identity?.getAttribute('title')).toBe('SPY');
    expect(identity?.textContent).toContain('SPY');
  });
});
