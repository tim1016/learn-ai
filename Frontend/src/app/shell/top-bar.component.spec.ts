import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';

import { TopBarComponent } from './top-bar.component';

describe('TopBarComponent', () => {
  it('renders a labelled header and Market Scope home link', async () => {
    await render(TopBarComponent);

    expect(screen.getByRole('banner', { name: 'Market Scope application' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Market Scope home' }).getAttribute('href')).toBe('/data-lab');
  });

  it('provides empty named regions for shell extensions', async () => {
    const { container } = await render(TopBarComponent);

    expect(container.querySelector('[data-shell-slot="breadcrumbs"]')).toBeTruthy();
    expect(container.querySelector('[data-shell-slot="account-cluster"]')).toBeTruthy();
    expect(container.querySelector('[data-shell-slot="connection"]')).toBeTruthy();
  });

  it('has no detectable accessibility violations', async () => {
    await render(TopBarComponent);

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
