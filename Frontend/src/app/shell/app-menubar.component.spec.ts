import { render, screen } from '@testing-library/angular';
import { Router, provideRouter } from '@angular/router';
import { TestBed } from '@angular/core/testing';
import { ChangeDetectionStrategy, Component } from '@angular/core';
import axe from 'axe-core';

import { AppMenubarComponent } from './app-menubar.component';
import { ACTIVE_GROUP_CLASS, APP_MENU } from './app-menu';

@Component({ template: '<p>Route body</p>', changeDetection: ChangeDetectionStrategy.OnPush })
class MenubarRouteComponent {}

const routes = [
  { path: 'data-lab', component: MenubarRouteComponent },
  { path: 'pricing-lab', component: MenubarRouteComponent },
];

describe('AppMenubarComponent', () => {
  it('renders a primary navigation landmark', async () => {
    await render(AppMenubarComponent, { providers: [provideRouter(routes)] });

    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeTruthy();
  });

  it('renders every canonical group as a trigger', async () => {
    const { container } = await render(AppMenubarComponent, { providers: [provideRouter(routes)] });

    for (const group of APP_MENU) {
      expect(screen.getAllByText(group.title).length).toBeGreaterThan(0);
    }
    expect(container.querySelectorAll('.p-menubar-root-list > li').length).toBe(APP_MENU.length);
  });

  it('moves the active highlight when the route changes', async () => {
    const { container, fixture } = await render(AppMenubarComponent, { providers: [provideRouter(routes)] });

    await TestBed.inject(Router).navigateByUrl('/pricing-lab');
    fixture.detectChanges();

    const active = container.querySelectorAll(`.${ACTIVE_GROUP_CLASS}`);
    expect(active.length).toBe(1);
    expect(active[0].textContent).toContain('Options');
  });

  it('has no detectable accessibility violations', async () => {
    await render(AppMenubarComponent, { providers: [provideRouter(routes)] });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
