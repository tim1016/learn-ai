import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { WorkspaceAction } from './workspace-action';
import { WorkspaceHeaderComponent, type WorkspaceBreadcrumbItem } from './workspace-header.component';

@Component({
  imports: [WorkspaceHeaderComponent],
  template: `
    <app-workspace-header
      title="SPY"
      [purpose]="purpose"
      [breadcrumb]="breadcrumb"
      [primaryAction]="primaryAction"
      [secondaryActions]="secondaryActions"
      (actionTriggered)="triggered.push($event)"
    >
      <a workspaceHelp href="/help/chart">How this works</a>
      <span workspaceFacts>Held 2020 to 2026</span>
    </app-workspace-header>
  `,
})
class HeaderHost {
  purpose: string | null = 'Daily bars for one instrument.';
  breadcrumb: readonly WorkspaceBreadcrumbItem[] = [
    { label: 'Markets', route: ['/markets'], queryParams: { page: '2' } },
    { label: 'Chart' },
  ];
  primaryAction: WorkspaceAction | null = null;
  secondaryActions: readonly WorkspaceAction[] = [];
  readonly triggered: string[] = [];
}

async function renderHeader(props: Partial<HeaderHost> = {}) {
  const view = await render(HeaderHost, {
    componentProperties: props,
    providers: [provideRouter([])],
  });
  return { host: view.fixture.componentInstance, view };
}

describe('WorkspaceHeaderComponent', () => {
  it('shows the trail back, the title, the purpose, the facts and the help link', async () => {
    await renderHeader();

    const trail = screen.getByRole('navigation', { name: 'Breadcrumb' });
    const parent = within(trail).getByRole('link', { name: 'Markets' });
    expect(parent.getAttribute('href')).toBe('/markets?page=2');
    // The current level is text, not a link back to itself.
    expect(within(trail).queryByRole('link', { name: 'Chart' })).toBeNull();
    expect(within(trail).getByText('Chart')).toBeTruthy();

    expect(screen.getByRole('heading', { level: 1, name: 'SPY' })).toBeTruthy();
    expect(screen.getByText('Daily bars for one instrument.')).toBeTruthy();
    expect(screen.getByText('Held 2020 to 2026')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'How this works' }).getAttribute('href')).toBe('/help/chart');
  });

  it('offers one primary action beside several secondary ones, and reports a clicked button', async () => {
    const { host, view } = await renderHeader({
      primaryAction: { id: 'run', label: 'Run backtest' },
      secondaryActions: [
        { id: 'save', label: 'Save' },
        { id: 'export', label: 'Export' },
        { id: 'build', label: 'Build dataset', route: ['/data', 'build'] },
      ],
    });

    expect(screen.getAllByRole('button').map((button) => button.textContent?.trim())).toEqual([
      'Save',
      'Export',
      'Run backtest',
    ]);
    expect(screen.getByRole('link', { name: 'Build dataset' }).getAttribute('href')).toBe('/data/build');
    expect(view.container.querySelectorAll('.workspace-action--primary')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Run backtest' }).classList).toContain('workspace-action--primary');

    await userEvent.click(screen.getByRole('button', { name: 'Run backtest' }));
    await userEvent.click(screen.getByRole('button', { name: 'Export' }));
    expect(host.triggered).toEqual(['run', 'export']);
  });

  it('keeps a disabled primary action from being triggered', async () => {
    const { host } = await renderHeader({
      primaryAction: { id: 'run', label: 'Run backtest', disabled: true },
    });

    const run = screen.getByRole('button', { name: 'Run backtest' }) as HTMLButtonElement;
    expect(run.disabled).toBe(true);
    await userEvent.click(run);
    expect(host.triggered).toEqual([]);
  });

  it('renders a read-only view with no action at all', async () => {
    const { view } = await renderHeader();

    expect(screen.getByRole('heading', { level: 1, name: 'SPY' })).toBeTruthy();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(view.container.querySelector('.workspace-header__actions')).toBeNull();
  });

  it('draws no empty purpose line, breadcrumb or slots when it is given none of them', async () => {
    const { view } = await renderHeader({ purpose: null, breadcrumb: [] });

    expect(screen.queryByRole('navigation', { name: 'Breadcrumb' })).toBeNull();
    expect(view.container.querySelector('.workspace-header__purpose')).toBeNull();
  });
});

@Component({
  imports: [WorkspaceHeaderComponent],
  template: `<app-workspace-header title="Golden Search" />`,
})
class MenuTrailHost {}

@Component({ template: '' })
class RouteStub {}

describe('WorkspaceHeaderComponent — trail from the menu', () => {
  it('leads back to the page\'s menu group when the host passes no breadcrumb', async () => {
    TestBed.configureTestingModule({
      providers: [provideRouter([{ path: 'golden-search/:studyId', component: RouteStub }])],
    });
    await TestBed.inject(Router).navigateByUrl('/golden-search/study-9');
    const fixture = TestBed.createComponent(MenuTrailHost);
    fixture.detectChanges();

    const links = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('nav[aria-label="Breadcrumb"] a')).map(
      (link) => `${link.textContent?.trim()} -> ${link.getAttribute('href')}`,
    );
    expect(links).toEqual(['Signal strategies -> /strategy-lab']);
  });
});
