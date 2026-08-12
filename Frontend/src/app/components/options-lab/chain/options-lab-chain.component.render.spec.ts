import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import { of } from 'rxjs';

import { MarketDataService } from '../../../services/market-data.service';
import { OptionsLabChainComponent } from './options-lab-chain.component';

describe('OptionsLabChainComponent render', () => {
  it('renders the toolbar underlying with the shared asset identity', async () => {
    const { container } = await render(OptionsLabChainComponent, {
      providers: [{
        provide: MarketDataService,
        useValue: {
          getOptionsExpirations: () => of([]),
          getStockSnapshot: () => of({ success: false, snapshot: null }),
        },
      }],
    });

    const identity = container.querySelector('.ticker-pill app-asset-identity');
    expect(identity).not.toBeNull();
    expect(identity?.getAttribute('title')).toBe('SPY');
    expect(screen.getByText('SPY')).toBeTruthy();
  });
});
