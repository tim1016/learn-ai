import { ChangeDetectionStrategy, Component } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { provideRouter, Router } from '@angular/router';
import axe from 'axe-core';

import { BreadcrumbComponent } from './breadcrumb.component';

@Component({ template: '', changeDetection: ChangeDetectionStrategy.OnPush })
class BreadcrumbTestRouteComponent {}

const BREADCRUMB_ROUTES = [{ path: '**', component: BreadcrumbTestRouteComponent }];

describe('BreadcrumbComponent', () => {
  it('renders menu ancestors and their canonical link targets', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/pricing-lab');
    fixture.detectChanges();

    expect(screen.getByRole('link', { name: 'Options' }).getAttribute('href')).toBe('/options-lab');
    expect(screen.getByRole('link', { name: 'Pricing Lab' }).getAttribute('href')).toBe('/pricing-lab');
  });

  it('stops at the deepest menu node for account-scoped routes', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/brokers/alpaca/accounts/PA9/bots/bot-7');
    fixture.detectChanges();

    expect(screen.getByRole('link', { name: 'Alpaca' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Bots' }).getAttribute('aria-current')).toBeNull();
    expect(screen.queryByText('bot-7')).toBeNull();
  });

  it('preserves current query state when the current crumb is followed', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/strategy-validation?strategy=ema_crossover_signal');
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('link', { name: 'Strategy Validation' }));
    await fixture.whenStable();

    expect(router.url).toBe('/strategy-validation?strategy=ema_crossover_signal');
  });

  it('closes the overflow menu after an ancestor crumb is selected', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/pricing-lab');
    fixture.detectChanges();
    const overflow = fixture.nativeElement.querySelector('.breadcrumb__overflow') as HTMLDetailsElement;
    overflow.open = true;

    fireEvent.click(fixture.nativeElement.querySelector('.breadcrumb__overflow a') as HTMLAnchorElement);

    expect(overflow.open).toBe(false);
  });

  it('renders nothing for a route outside the menu', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/jobs-demo');
    fixture.detectChanges();

    expect(screen.queryByRole('navigation', { name: 'Breadcrumb' })).toBeNull();
  });

  it('provides the accessible overflow control used on narrow layouts', async () => {
    const { fixture, router } = await renderBreadcrumb();

    await router.navigateByUrl('/pricing-lab');
    fixture.detectChanges();

    expect(screen.getByRole('navigation', { name: 'Breadcrumb' })).toBeTruthy();
    expect(fixture.nativeElement.querySelector('summary[aria-label="Show earlier breadcrumbs"]')).toBeTruthy();
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
