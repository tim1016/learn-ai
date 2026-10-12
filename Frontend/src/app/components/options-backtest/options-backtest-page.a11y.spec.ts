import { fireEvent, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { flip } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';

describe('the page for a screen reader', () => {
  it('has no accessibility violations with a setup open, its rules on, and a problem showing', async () => {
    await renderPage(fakeService(vi));
    fireEvent.click(await within(screen.getByRole('complementary', { name: 'Saved setups' })).findByRole('button', { name: /^SPY premium momentum, one re-entry/ }));
    await screen.findByRole('article', { name: 'Leg 2' });
    flip('Leg 1 simple momentum');

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.html).join(' | ')}`)).toEqual([]);
  });
});
