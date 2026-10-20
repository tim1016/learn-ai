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
  it('renders the default bar: labelled header, centered Botasur home link, no mode tint, no accessibility violations', async () => {
    const { container } = await render(TopBarComponent, { providers: [provideRouter([])] });

    expect.soft(screen.getByRole('banner', { name: 'Botasur application' }), 'renders a labelled header').toBeTruthy();
    expect.soft(screen.getByRole('link', { name: 'Botasur home' }).getAttribute('href'), 'links the Botasur home to /data-lab').toBe('/data-lab');
    expect.soft(screen.getByText('Botasur'), 'renders the Botasur name').toBeTruthy();
    expect.soft(container.querySelector('.top-bar__brand-lockup')?.getAttribute('src'), 'renders the brand lockup mark').toBe(
      '/assets/brand/botasur-header-mark.svg',
    );
    const unknownClasses = container.querySelector('.top-bar')?.classList;
    expect.soft(unknownClasses?.contains('top-bar--paper'), 'paints no paper tint for an unknown accountMode').toBe(false);
    expect.soft(unknownClasses?.contains('top-bar--live'), 'paints no live tint for an unknown accountMode').toBe(false);

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect.soft(results.violations, 'has no detectable accessibility violations').toEqual([]);
  });

  it.each<['paper' | 'live', string]>([
    ['paper', 'top-bar--paper'],
    ['live', 'top-bar--live'],
  ])('paints the solid tint for accountMode %s', async (accountMode, tintClass) => {
    const view = await render(TopBarComponent, {
      providers: [provideRouter([])],
      componentInputs: { accountMode },
    });
    expect(view.container.querySelector('.top-bar')?.classList.contains(tintClass)).toBe(true);
  });

  it('projects each shell extension through its named region', async () => {
    const { container } = await render(TopBarProjectionHostComponent, { providers: [provideRouter([])] });

    expect(container.querySelector('[data-shell-slot="nav"]')?.textContent).toContain('Primary navigation');
    expect(container.querySelector('[data-shell-slot="connection"]')?.textContent).toContain('Connection control');
  });

});
