import { Component } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import axe from 'axe-core';

import { TopBarComponent } from './top-bar.component';

@Component({
  imports: [TopBarComponent],
  template: `
    <app-top-bar>
      <span shell-breadcrumbs>Breadcrumbs</span>
      <span shell-account-cluster>Account context</span>
      <span shell-connection>Connection control</span>
    </app-top-bar>
  `,
})
class TopBarProjectionHostComponent {}

describe('TopBarComponent', () => {
  it('renders a labelled header and Market Scope home link', async () => {
    await render(TopBarComponent, { providers: [provideRouter([])] });

    expect(screen.getByRole('banner', { name: 'Market Scope application' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Market Scope home' }).getAttribute('href')).toBe('/data-lab');
  });

  it('provides named regions for shell extensions', async () => {
    const { container } = await render(TopBarComponent, { providers: [provideRouter([])] });

    expect(container.querySelector('[data-shell-slot="breadcrumbs"]')).toBeTruthy();
    expect(container.querySelector('[data-shell-slot="account-cluster"]')).toBeTruthy();
    expect(container.querySelector('[data-shell-slot="connection"]')).toBeTruthy();
  });

  it('projects each shell extension through its named region', async () => {
    const { container } = await render(TopBarProjectionHostComponent);

    expect(container.querySelector('[data-shell-slot="breadcrumbs"]')?.textContent).toContain('Breadcrumbs');
    expect(container.querySelector('[data-shell-slot="account-cluster"]')?.textContent).toContain('Account context');
    expect(container.querySelector('[data-shell-slot="connection"]')?.textContent).toContain('Connection control');
  });

  it('has no detectable accessibility violations', async () => {
    await render(TopBarComponent, { providers: [provideRouter([])] });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
