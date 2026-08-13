import { ChangeDetectionStrategy, Component } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { provideRouter, Router } from '@angular/router';
import axe from 'axe-core';

import { BreadcrumbComponent } from './breadcrumb.component';

@Component({ template: '', changeDetection: ChangeDetectionStrategy.OnPush })
class BreadcrumbTestRouteComponent {}

const BREADCRUMB_ROUTES = [{ path: '**', component: BreadcrumbTestRouteComponent }];

describe('BreadcrumbComponent', () => {
  it('renders the menu ancestor and omits the current page crumb', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/pricing-lab');
    fixture.detectChanges();

    expect(screen.getByRole('link', { name: 'Options' }).getAttribute('href')).toBe('/options-lab');
    expect(screen.queryByRole('link', { name: 'Pricing Lab' })).toBeNull();
  });

  it('stops at the deepest menu node for account-scoped routes', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/brokers/alpaca/accounts/PA9/bots/bot-7');
    fixture.detectChanges();

    expect(screen.getByRole('link', { name: 'Alpaca' })).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Bots' })).toBeNull();
    expect(screen.queryByText('bot-7')).toBeNull();
  });

  it('navigates an ancestor to its canonical route without retaining the page query', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/brokers/alpaca?deploy=');
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('link', { name: 'Alpaca' }));
    await fixture.whenStable();

    expect(router.url).toBe('/brokers/alpaca');
  });

  it('does not mark an ancestor link as the current page', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/pricing-lab');
    fixture.detectChanges();

    expect(screen.getByRole('link', { name: 'Options' }).getAttribute('aria-current')).toBeNull();
  });

  it('renders nothing for a route outside the menu', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/jobs-demo');
    fixture.detectChanges();

    expect(screen.queryByRole('navigation', { name: 'Breadcrumb' })).toBeNull();
  });

  it('has no detectable accessibility violations', async () => {
    const { fixture, router } = await renderBreadcrumb();
    await router.navigateByUrl('/pricing-lab');
    fixture.detectChanges();

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});

async function renderBreadcrumb() {
  const result = await render(BreadcrumbComponent, { providers: [provideRouter(BREADCRUMB_ROUTES)] });
  return { fixture: result.fixture, router: result.fixture.debugElement.injector.get(Router) };
}
