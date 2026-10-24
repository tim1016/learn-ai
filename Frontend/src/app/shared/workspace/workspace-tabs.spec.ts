import { describe, expect, it } from 'vitest';

import { defineWorkspaceTabs } from './workspace-tabs';

describe('defineWorkspaceTabs', () => {
  const tabs = defineWorkspaceTabs(
    { home: 'Home', logs: 'Logs', deploy: 'Deploy a bot' },
    ['home', 'logs'],
  );

  it('lists the strip in its declared order and names a routed page the strip leaves out', () => {
    expect(tabs.tabs).toEqual([
      { id: 'home', label: 'Home' },
      { id: 'logs', label: 'Logs' },
    ]);
    // Deploy opens from a header button, not the strip, but its window title
    // still reads from the same declaration.
    expect(tabs.label('deploy')).toBe('Deploy a bot');
  });

  it('refuses a tab listed twice rather than render two links to one place', () => {
    expect(() => defineWorkspaceTabs({ home: 'Home', logs: 'Logs' }, ['home', 'logs', 'home'])).toThrow(/home/);
  });
});
