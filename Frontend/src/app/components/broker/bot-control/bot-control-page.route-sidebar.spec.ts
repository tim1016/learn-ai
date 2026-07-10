import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HostRunnerStartRequest } from '../../../api/live-runs.types';
import {
  makeHostRunnerProcess,
  makeStatus,
} from './bot-control-page.fixtures';
import {
  flush,
  installBotControlPageTestStubs,
  setupBotControlPage,
  setupBotControlSidebarHost,
} from './bot-control-page.testing';

describe('BotControlPageComponent route and sidebar behavior', () => {
  beforeEach(() => installBotControlPageTestStubs());

  afterEach(() => {
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it('renders the active bot host-runner warning through the sidebar consumer', async () => {
    const { element } = await setupBotControlSidebarHost();
    const notice = element.querySelector('[data-testid="sidebar-host-runner-notice"]');
    expect(notice?.textContent).toContain('Start the host runner before trading this bot.');
    expect(notice?.textContent).toContain('make broker-runner');
    expect(element.querySelector('[data-testid="bot-control-host-runner-banner"]')).toBeNull();
  });

  it('renders an invalid live-binding sidebar notice with a bind-again action', async () => {
    const request: HostRunnerStartRequest = {
      readonly: false,
      hydrate_policy: 'require',
      strategy: 'deployment_validation',
      max_orders_per_day: 2,
      ibkr_host: '127.0.0.1',
    };
    const { fixture, element, liveRuns } = await setupBotControlSidebarHost({
      routeId: 'DEPVALJUL1',
      status: makeStatus({
        id: 'DEPVALJUL1',
        hostState: 'WAITING_FOR_HOST',
        hostNotice: 'Trading was requested, but this bot process has not started yet.',
        startCapabilityEnabled: true,
        startRunId: 'run-bind',
        startRequest: request,
      }),
      mutationResponses: {
        startHostRunner: {
          accepted: true,
          process: makeHostRunnerProcess(),
        },
      },
    });

    const notice = element.querySelector('[data-testid="sidebar-host-runner-notice"]');
    const action = element.querySelector<HTMLButtonElement>('[data-testid="sidebar-host-runner-action"]');
    expect(notice?.textContent).toContain('Live binding invalid.');
    expect(action?.textContent?.trim()).toBe('Bind again');

    action?.click();
    await flush(fixture);

    expect(liveRuns.startHostRunner).toHaveBeenCalledWith('run-bind', request);
  });

  it('never starts a status polling loop in the page component', async () => {
    vi.useFakeTimers();
    const { fixture, liveRuns } = await setupBotControlPage();

    await vi.advanceTimersByTimeAsync(12_000);
    fixture.detectChanges();

    expect(liveRuns.getInstanceStatus).not.toHaveBeenCalled();
  });
});
