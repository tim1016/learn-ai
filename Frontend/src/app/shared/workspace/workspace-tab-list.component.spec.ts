import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { WorkspaceTabListComponent, type WorkspaceTabItem } from './workspace-tab-list.component';

const TABS: readonly WorkspaceTabItem[] = [
  { id: 'home', label: 'Home', route: ['/ws', 'home'] },
  { id: 'activity', label: 'Activity', route: null, unavailableReason: 'Needs a confirmed account.' },
  { id: 'settings', label: 'Settings', route: ['/ws', 'settings'] },
];

async function renderTabs(activeId: string | null) {
  await render(WorkspaceTabListComponent, {
    inputs: { tabs: TABS, activeId, label: 'Workspace sections' },
    providers: [provideRouter([])],
  });
}

describe('WorkspaceTabListComponent', () => {
  it('links each tab it has an address for and marks only the open one', async () => {
    await renderTabs('settings');

    const nav = screen.getByRole('navigation', { name: 'Workspace sections' });
    expect(nav).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Home' }).getAttribute('href')).toBe('/ws/home');
    expect(screen.getByRole('link', { name: 'Settings' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByRole('link', { name: 'Home' }).getAttribute('aria-current')).toBeNull();
  });

  it('keeps a tab it has no address for in the strip, inert, and says why', async () => {
    await renderTabs('home');

    expect(screen.queryByRole('link', { name: /Activity/ })).toBeNull();
    expect(screen.getByText('Activity').getAttribute('aria-disabled')).toBe('true');
    expect(screen.getByText(/Needs a confirmed account/)).toBeTruthy();
  });

  it('carries a tab’s query on its link and a short state beside its label', async () => {
    await render(WorkspaceTabListComponent, {
      inputs: {
        tabs: [{ id: 'backtest', label: 'Backtest', route: ['/ws', 'backtest'], queryParams: { run: '91' }, badge: 'running' }],
        activeId: 'sweep',
        label: 'Workspace sections',
      },
      providers: [provideRouter([])],
    });

    const link = screen.getByRole('link', { name: /Backtest/ });
    expect(link.getAttribute('href')).toBe('/ws/backtest?run=91');
    expect(link.textContent).toContain('running');
  });

  it('marks no tab when none is open', async () => {
    await renderTabs(null);

    for (const name of ['Home', 'Settings']) {
      expect(screen.getByRole('link', { name }).getAttribute('aria-current')).toBeNull();
    }
  });
});
