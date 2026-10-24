import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { GoldenFixturesService } from '../../services/golden-fixtures.service';
import type { FixtureSummary, GoldenFixturesCatalog } from '../../services/golden-fixtures.types';
import { GoldenFixturesCatalogComponent } from './golden-fixtures-catalog.component';

function fixture(overrides: Partial<FixtureSummary>): FixtureSummary {
  return {
    id: 'BS-001',
    name: 'Black-Scholes European Call Price',
    category: 'options-pricing',
    canonical_module: 'app.options.black_scholes',
    canonical_callable: 'call_price',
    reference_kind: 'external_reference',
    is_certified: true,
    status: 'active',
    active_version: 1,
    tolerance: { atol: 1e-10, rtol: 0, note: '' },
    ...overrides,
  };
}

async function renderCatalog(read: () => Promise<GoldenFixturesCatalog>) {
  return render(GoldenFixturesCatalogComponent, {
    providers: [{ provide: GoldenFixturesService, useValue: { getCatalog: read } }],
  });
}

describe('GoldenFixturesCatalogComponent', () => {
  it('groups the fixtures by category and says which are certified, with the last validation run', async () => {
    await renderCatalog(async () => ({
      fixtures: [
        fixture({}),
        fixture({ id: 'EMA-001', name: 'EMA 10 on SPY', category: 'indicators', reference_kind: 'internal_regression', is_certified: false }),
      ],
      validation: { generated_at: '2026-10-23T14:43:00+00:00', passed: 78, failed: 0, errors: 0, status: 'ok' },
    }));

    const options = await screen.findByRole('table', { name: 'Options Pricing fixtures' });
    expect(within(options).getByText('BS-001')).toBeTruthy();
    expect(within(options).getByLabelText('Oracle: External Reference')).toBeTruthy();
    expect(within(options).getByText('1e-10')).toBeTruthy();
    const indicators = screen.getByRole('table', { name: 'Indicators fixtures' });
    expect(within(indicators).getByLabelText('Oracle: Regression Pinned')).toBeTruthy();

    const stats = screen.getByRole('region', { name: 'Fixture statistics' });
    expect(within(stats).getByText('Total Fixtures').previousElementSibling?.textContent).toBe('2');
    expect(within(stats).getByText('Certified').previousElementSibling?.textContent).toBe('1');
    expect(within(stats).getByText('Tests Passed').previousElementSibling?.textContent).toBe('78');
    expect(within(stats).getByText('Last run: 2026-10-23T14:43')).toBeTruthy();
  });

  it('shows a failing validation run as failed, not as passed', async () => {
    await renderCatalog(async () => ({
      fixtures: [fixture({ status: 'breach' })],
      validation: { generated_at: '2026-10-23T14:43:00+00:00', passed: 77, failed: 1, errors: 0, status: 'failed' },
    }));

    const stats = await screen.findByRole('region', { name: 'Fixture statistics' });
    expect(within(stats).getByText('Failed').previousElementSibling?.textContent).toBe('1');
    expect(screen.getByText('breach')).toBeTruthy();
  });

  it('says the catalog could not be read rather than showing an empty one', async () => {
    await renderCatalog(async () => {
      throw new Error('unreachable');
    });

    expect((await screen.findByRole('alert')).textContent).toContain('Failed to load fixture catalog');
    expect(screen.queryByRole('region', { name: 'Fixture statistics' })).toBeNull();
  });
});
