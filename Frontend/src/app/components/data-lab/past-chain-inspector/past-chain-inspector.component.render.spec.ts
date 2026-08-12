import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { PastChainInspectorComponent } from './past-chain-inspector.component';

describe('PastChainInspectorComponent render', () => {
  it('renders the selected underlying with the shared asset identity', async () => {
    const { container } = await render(PastChainInspectorComponent, {
      inputs: { ticker: 'NVDA', analysisDate: '2026-05-15' },
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });

    const identity = container.querySelector('.card-subtitle app-asset-identity');
    expect(identity).not.toBeNull();
    expect(identity?.getAttribute('title')).toBe('NVDA');
    expect(screen.getByText('NVDA')).toBeTruthy();
  });
});
