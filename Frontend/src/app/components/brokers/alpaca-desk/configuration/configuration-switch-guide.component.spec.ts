import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { ConfigurationSwitchGuideComponent } from './configuration-switch-guide.component';

describe('ConfigurationSwitchGuideComponent', () => {
  it('shows the complete account-switch workflow and safety boundary', async () => {
    await render(ConfigurationSwitchGuideComponent, {
      inputs: { restartCommand: 'podman compose restart alpaca-paper-clerk' },
    });

    expect(screen.getByRole('heading', { name: 'Switch between Paper and Live' })).toBeTruthy();
    // #2183: the heading itself carries the eyebrow look; the separate
    // "Every account switch" label above it is retired.
    expect(screen.queryByText('Every account switch')).toBeNull();
    expect(screen.getAllByRole('listitem')).toHaveLength(4);
    expect(screen.getByText(/Refreshing or reopening the browser does not apply/)).toBeTruthy();
    expect(screen.getByText(/never retargets, arms, or launches an existing bot/)).toBeTruthy();
  });

  it('shows the command the backend authored for this lane, not a built-in one', async () => {
    await render(ConfigurationSwitchGuideComponent, {
      inputs: { restartCommand: 'podman compose restart alpaca-live-clerk' },
    });

    const command = screen.getByText('podman compose restart alpaca-live-clerk');
    expect(command.tagName).toBe('CODE');
    expect(screen.getByRole('button', { name: 'Copy worker restart command' })).toBeTruthy();
  });

  it('offers nothing to copy when the deployment declared no worker service', async () => {
    await render(ConfigurationSwitchGuideComponent, { inputs: { restartCommand: null } });

    expect(screen.getByText(/did not declare this lane's worker service/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Copy worker restart command' })).toBeNull();
    expect(screen.queryByText(/podman compose restart/)).toBeNull();
  });

  it('says nothing about the worker service while the desk state is unknown', async () => {
    await render(ConfigurationSwitchGuideComponent, { inputs: { restartCommand: undefined } });

    expect(screen.getByText('Restart the worker')).toBeTruthy();
    expect(screen.queryByText(/did not declare this lane's worker service/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Copy worker restart command' })).toBeNull();
    expect(screen.queryByText(/podman compose restart/)).toBeNull();
  });

  it('has no detectable accessibility violations with a command', async () => {
    await render(ConfigurationSwitchGuideComponent, {
      inputs: { restartCommand: 'podman compose restart alpaca-paper-clerk' },
    });

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });

  it('has no detectable accessibility violations without a command', async () => {
    await render(ConfigurationSwitchGuideComponent, { inputs: { restartCommand: null } });

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });

  it('has no detectable accessibility violations while the command is unknown', async () => {
    await render(ConfigurationSwitchGuideComponent, { inputs: { restartCommand: undefined } });

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
