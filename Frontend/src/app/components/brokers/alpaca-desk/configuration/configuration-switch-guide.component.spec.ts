import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { ConfigurationSwitchGuideComponent } from './configuration-switch-guide.component';

describe('ConfigurationSwitchGuideComponent', () => {
  it('shows the complete account-switch workflow and safety boundary', async () => {
    await render(ConfigurationSwitchGuideComponent);

    expect(screen.getByRole('heading', { name: 'Switch between Paper and Live' })).toBeTruthy();
    expect(screen.getAllByRole('listitem')).toHaveLength(4);
    expect(screen.getByText('podman compose restart python-service')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Copy worker restart command' })).toBeTruthy();
    expect(screen.getByText(/Refreshing or reopening the browser does not apply/)).toBeTruthy();
    expect(screen.getByText(/never retargets, arms, or launches an existing bot/)).toBeTruthy();
  });

  it('has no detectable accessibility violations', async () => {
    await render(ConfigurationSwitchGuideComponent);

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
