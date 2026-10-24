import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { routes } from '../../app.routes';
import { STRATEGY_FINDER_UNAVAILABLE_REASON } from './strategy-finder-unavailable.component';

describe('Strategy finder page', () => {
  it('opens on a direct load as a not-available page that says why, with its tab marked the same way', async () => {
    TestBed.configureTestingModule({ providers: [provideRouter(routes)] });
    const harness = await RouterTestingHarness.create();

    await harness.navigateByUrl('/options-setups/analysis/finder');
    harness.detectChanges();

    const root = harness.routeNativeElement;
    if (root === null) throw new Error('The analysis route did not render.');
    const page = within(root);
    expect(page.getByRole('heading', { name: 'Strategy finder is not available' })).toBeTruthy();
    expect(page.getByText(STRATEGY_FINDER_UNAVAILABLE_REASON)).toBeTruthy();
    expect(page.getByRole('link', { name: 'Strategy Finder (not available)' }).getAttribute('aria-current')).toBe('page');
  });
});
