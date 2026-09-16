import { Component } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import axe from 'axe-core';

import { TopBarComponent } from './top-bar.component';

@Component({
  imports: [TopBarComponent],
  template: `
    <app-top-bar>
      <span shell-nav>Primary navigation</span>
      <span shell-connection>Connection control</span>
    </app-top-bar>
  `,
})
class TopBarProjectionHostComponent {}

describe('TopBarComponent', () => {
  it('renders a labelled header and centered Botasur home link', async () => {
    const { container } = await render(TopBarComponent, { providers: [provideRouter([])] });

    expect(screen.getByRole('banner', { name: 'Botasur application' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Botasur home' }).getAttribute('href')).toBe('/data-lab');
    expect(screen.getByText('Botasur')).toBeTruthy();
    expect(container.querySelector('.top-bar__brand-lockup')?.getAttribute('src')).toBe(
      '/assets/brand/botasur-header-mark.svg',
    );
  });

  it('renders the header mode-neutral — the per-lane pills own the account-mode signal', async () => {
    const { container } = await render(TopBarComponent, { providers: [provideRouter([])] });

    expect(screen.getByRole('banner').classList.contains('top-bar--paper')).toBe(false);
    expect(screen.getByRole('banner').classList.contains('top-bar--live')).toBe(false);
    expect(container.querySelector('.top-bar--paper, .top-bar--live')).toBeNull();
  });

  it('provides named regions for shell extensions', async () => {
    const { container } = await render(TopBarComponent, { providers: [provideRouter([])] });

    expect(container.querySelector('[data-shell-slot="nav"]')).toBeTruthy();
    expect(container.querySelector('[data-shell-slot="connection"]')).toBeTruthy();
  });

  it('no longer carries the retired breadcrumb and account-cluster regions', async () => {
    const { container } = await render(TopBarComponent, { providers: [provideRouter([])] });

    expect(container.querySelector('[data-shell-slot="breadcrumbs"]')).toBeNull();
    expect(container.querySelector('[data-shell-slot="account-cluster"]')).toBeNull();
  });

  it('projects each shell extension through its named region', async () => {
    const { container } = await render(TopBarProjectionHostComponent, { providers: [provideRouter([])] });

    expect(container.querySelector('[data-shell-slot="nav"]')?.textContent).toContain('Primary navigation');
    expect(container.querySelector('[data-shell-slot="connection"]')?.textContent).toContain('Connection control');
  });

  it('has no detectable accessibility violations', async () => {
    await render(TopBarComponent, { providers: [provideRouter([])] });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

});
