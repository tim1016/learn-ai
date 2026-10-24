import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen } from '@testing-library/angular';
import { beforeEach, describe, expect, it } from 'vitest';

import { RESEARCH_ROUTES } from '../research.routes';

describe('Research demonstrations', () => {
  let harness: RouterTestingHarness;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([{ path: 'research', children: RESEARCH_ROUTES }]),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    harness = await RouterTestingHarness.create();
  });

  it.each([
    ['/research/demos/cross-asset', 'Cross-asset demo'],
    ['/research/demos/regimes', 'Regimes demo'],
    ['/research/demos/edge-score', 'Edge score demo'],
    ['/research/demos/trade-simulator', 'Trade simulator demo'],
  ])('heads %s with its own name and labels what it shows as sample data', async (url, title) => {
    await harness.navigateByUrl(url);

    expect.soft(screen.getByRole('heading', { level: 1, name: title })).toBeTruthy();
    expect.soft(screen.getAllByTestId('sample-data-banner').length, 'drawn from the data it renders').toBeGreaterThan(0);
  });

  it('draws the Edge score and Trade simulator in the page, not as drawers over it', async () => {
    await harness.navigateByUrl('/research/demos/edge-score');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText('EDGE SCORE INSPECTOR')).toBeTruthy();

    await harness.navigateByUrl('/research/demos/trade-simulator');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText(/TRADE SIMULATOR/)).toBeTruthy();
  });
});
