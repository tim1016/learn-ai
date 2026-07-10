import { convertToParamMap, type ParamMap } from '@angular/router';
import { Subject } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import type { HostRunnerStartRequest } from '../../../api/live-runs.types';
import { environment } from '../../../../environments/environment';
import {
  makeHostRunnerProcess,
  makeStatus,
} from './bot-control-page.fixtures';
import {
  deferred,
  flush,
  installBotControlPageTestStubs,
  setupBotControlPage,
  setupBotControlSidebarHost,
} from './bot-control-page.testing';

describe('BotControlPageComponent route and sidebar behavior', () => {
  beforeEach(() => {
    installBotControlPageTestStubs();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    environment.flags.botCockpitStateStream = false;
    window.localStorage.clear();
  });

  it('resets typed HALT and ignores a stale confirm when the route changes to another bot', async () => {
    const paramMap = new Subject<ParamMap>();
    const { fixture, component, liveRuns } = await setupBotControlPage({
      routeParamMap$: paramMap,
      status: makeStatus({ markPoisonedEnabled: true }),
    });

    paramMap.next(convertToParamMap({ id: 'bot-a' }));
    await flush(fixture);

    component.openTypedHalt();
    component.busyAction.set('start_process');
    fixture.detectChanges();
    expect(component.typedHaltOpen()).toBe(true);

    paramMap.next(convertToParamMap({ id: 'bot-b' }));
    await flush(fixture);

    expect(component.typedHaltOpen()).toBe(false);
    expect(component.busyAction()).toBeNull();
    await component.confirmTypedHalt();
    expect(liveRuns.issueInstanceCommand).not.toHaveBeenCalled();
  });

  it('renders the active bot host-runner warning through the sidebar consumer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { element: el } = await setupBotControlSidebarHost();
    const sidebarNotice = el.querySelector('[data-testid="sidebar-host-runner-notice"]');
    expect(sidebarNotice?.textContent).toContain('Start the host runner before trading this bot.');
    expect(sidebarNotice?.textContent).toContain('make broker-runner');
    expect(el.querySelector('[data-testid="bot-control-host-runner-banner"]')).toBeNull();
  });

  it('renders an invalid live-binding sidebar notice with a bind-again action', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const request: HostRunnerStartRequest = {
      readonly: false,
      hydrate_policy: 'require',
      strategy: 'deployment_validation',
      max_orders_per_day: 2,
      ibkr_host: '127.0.0.1',
    };
    const { fixture, element: el, liveRuns } = await setupBotControlSidebarHost({
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

    const sidebarNotice = el.querySelector('[data-testid="sidebar-host-runner-notice"]');
    const action = el.querySelector<HTMLButtonElement>('[data-testid="sidebar-host-runner-action"]');
    expect(sidebarNotice?.textContent).toContain('Live binding invalid.');
    expect(sidebarNotice?.textContent).toContain('Trading was requested, but this bot process has not started yet.');
    expect(action?.textContent?.trim()).toBe('Bind again');

    action?.click();
    await flush(fixture);

    expect(liveRuns.startHostRunner).toHaveBeenCalledWith('run-bind', request);
  });

  it('refreshes status on the serialized poll loop', async () => {
    vi.useFakeTimers();
    const { fixture, liveRuns } = await setupBotControlPage();
    expect(liveRuns.getInstanceStatus).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(4_000);
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();

    expect(liveRuns.getInstanceStatus).toHaveBeenCalledTimes(2);
  });

  it('disables the four-second poll when the state-stream flag is enabled', async () => {
    class StubEventSource {
      static instances: StubEventSource[] = [];

      constructor(readonly url: string) {
        StubEventSource.instances.push(this);
      }

      addEventListener(): void {}
      close(): void {}
    }

    vi.useFakeTimers();
    vi.stubGlobal('EventSource', StubEventSource);
    environment.flags.botCockpitStateStream = true;
    const { fixture, liveRuns } = await setupBotControlPage();
    await flush(fixture);

    await vi.advanceTimersByTimeAsync(8_000);
    await flush(fixture);

    expect(liveRuns.getInstanceStatus).toHaveBeenCalledTimes(1);
    expect(StubEventSource.instances).toHaveLength(1);
  });

  it('ignores stale status responses after the route changes to another bot', async () => {
    const paramMap = new Subject<ParamMap>();
    const first = deferred<LiveInstanceStatus>();
    const second = deferred<LiveInstanceStatus>();
    const { fixture, element } = await setupBotControlSidebarHost({
      routeParamMap$: paramMap,
      statusResolver: (id) => id === 'bot-a' ? first.promise : second.promise,
    });

    paramMap.next(convertToParamMap({ id: 'bot-a' }));
    paramMap.next(convertToParamMap({ id: 'bot-b' }));
    second.resolve(makeStatus({ id: 'bot-b', hostNotice: 'B runner is unreachable.' }));
    await flush(fixture);
    first.resolve(makeStatus({ id: 'bot-a', hostNotice: 'A runner is unreachable.' }));
    await flush(fixture);

    const sidebarNotice = element.querySelector('[data-testid="sidebar-host-runner-notice"]');
    expect(sidebarNotice?.textContent).toContain('B runner is unreachable.');
  });
});
