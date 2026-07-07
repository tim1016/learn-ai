import { ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { describe, expect, it, vi } from 'vitest';
import { BrokerBannerComponent } from './broker-banner.component';
import { BrokerHealthService } from '../services/broker-health.service';
import { LiveRunsService } from '../services/live-runs.service';
import { ActiveBotSidebarNoticeService } from './active-bot-sidebar-notice.service';
import type { IbkrConnectionHealth } from '../api/broker-models';

function health(overrides: Partial<IbkrConnectionHealth> = {}): IbkrConnectionHealth {
  return {
    mode: 'paper',
    host: '127.0.0.1',
    port: 4002,
    client_id: 1,
    connected: true,
    account_id: 'DU1234567',
    is_paper: true,
    server_version: 178,
    fetched_at_ms: 1_700_000_000_000,
    connection_state: 'connected',
    last_transition_ms: 1_700_000_000_000,
    ...overrides,
  };
}

class FakeBrokerHealthService {
  readonly health = signal<IbkrConnectionHealth | null>(null);
  readonly bannerState = signal<
    'paper' | 'live' | 'degraded' | 'disconnected' | 'disabled-host-runner-active' | null
  >(null);
  readonly lifecycleAction = signal<'connect' | 'disconnect' | 'reconnect' | null>(null);
  connect = vi.fn().mockResolvedValue(undefined);
  disconnect = vi.fn().mockResolvedValue(undefined);
}

class FakeLiveRunsService {
  startHostRunner = vi.fn().mockResolvedValue(undefined);
}

function setup() {
  const brokerHealth = new FakeBrokerHealthService();
  const liveRuns = new FakeLiveRunsService();
  const activeBotNotice = new ActiveBotSidebarNoticeService();
  TestBed.configureTestingModule({
    imports: [BrokerBannerComponent],
    providers: [
      { provide: BrokerHealthService, useValue: brokerHealth },
      { provide: LiveRunsService, useValue: liveRuns },
      { provide: ActiveBotSidebarNoticeService, useValue: activeBotNotice },
    ],
  });
  const fixture = TestBed.createComponent(BrokerBannerComponent);
  fixture.detectChanges();
  return { fixture, brokerHealth, liveRuns, activeBotNotice };
}

function toggle(fixture: ComponentFixture<BrokerBannerComponent>): HTMLButtonElement | null {
  return fixture.nativeElement.querySelector('.broker-toggle') as HTMLButtonElement | null;
}

function bindAgainRequest() {
  return {
    readonly: false,
    hydrate_policy: 'require' as const,
    strategy: 'deployment_validation',
    max_orders_per_day: 2,
    ibkr_host: '127.0.0.1',
  };
}

describe('BrokerBannerComponent', () => {
  it('renders no control before broker health has loaded', () => {
    const { fixture } = setup();
    expect(fixture.nativeElement.querySelector('.broker-banner')).toBeNull();
  });

  it('connects from the sidebar toggle when disconnected', async () => {
    const { fixture, brokerHealth } = setup();
    brokerHealth.bannerState.set('disconnected');
    brokerHealth.health.set(health({ connected: false, is_paper: null }));
    fixture.detectChanges();

    const button = toggle(fixture);
    expect(button?.textContent?.trim()).toBe('Connect');
    button?.click();

    expect(brokerHealth.connect).toHaveBeenCalledTimes(1);
    expect(brokerHealth.disconnect).not.toHaveBeenCalled();
  });

  it('disconnects from the sidebar toggle when connected', async () => {
    const { fixture, brokerHealth } = setup();
    brokerHealth.bannerState.set('paper');
    brokerHealth.health.set(health());
    fixture.detectChanges();

    const button = toggle(fixture);
    expect(button?.getAttribute('aria-pressed')).toBe('true');
    expect(button?.textContent?.trim()).toBe('Disconnect');
    button?.click();

    expect(brokerHealth.disconnect).toHaveBeenCalledTimes(1);
    expect(brokerHealth.connect).not.toHaveBeenCalled();
  });

  it('suppresses the toggle when the host runner owns IBKR', () => {
    const { fixture, brokerHealth } = setup();
    brokerHealth.bannerState.set('disabled-host-runner-active');
    brokerHealth.health.set(health({ disabled: true, connected: false }));
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Host-owned');
    expect(toggle(fixture)).toBeNull();
  });

  it('renders hard-down recovery as a degraded broker state', () => {
    const { fixture, brokerHealth } = setup();
    brokerHealth.bannerState.set('degraded');
    brokerHealth.health.set(
      health({
        connected: false,
        connection_state: 'hard_down',
        condition: {
          code: 'DATA_PLANE_BROKER_HARD_DOWN',
          severity: 'critical',
          title: 'Data-plane broker session down',
          summary:
            'IB Gateway/TWS may be logged in, but the FastAPI data-plane IBKR client is not connected.',
          remediation: 'Use the IBKR Connect/Reconnect control after confirming Gateway API access is enabled.',
        },
      }),
    );
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Data-plane broker session down');
    expect(fixture.nativeElement.textContent).toContain('FastAPI data-plane IBKR client is not connected');
  });

  it('renders active bot host-runner warning above the IBKR banner', () => {
    const { fixture, brokerHealth, activeBotNotice } = setup();
    activeBotNotice.setNotice({
      instanceId: 'DEPVAL-DIA-20260626',
      kind: 'host-runner-unreachable',
      summary: 'Warning, host runner unreachable.',
      message: 'The bot service is offline.',
      command: 'make host-runner',
      action: null,
    });
    brokerHealth.bannerState.set('disconnected');
    brokerHealth.health.set(health({ connected: false, is_paper: null }));
    fixture.detectChanges();

    const notice = fixture.nativeElement.querySelector(
      '[data-testid="sidebar-host-runner-notice"]',
    ) as HTMLElement | null;
    const banner = fixture.nativeElement.querySelector('.broker-banner') as HTMLElement | null;
    expect(notice?.querySelector('summary')?.textContent).toContain('Warning, host runner unreachable.');
    expect(notice?.textContent).toContain('The bot service is offline.');
    expect(notice?.compareDocumentPosition(banner as Node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it('starts the host process from an invalid live-binding sidebar action', async () => {
    const { fixture, activeBotNotice, liveRuns } = setup();
    const request = bindAgainRequest();
    activeBotNotice.setNotice({
      instanceId: 'DEPVALJUL1',
      kind: 'live-binding-invalid',
      summary: 'Live binding invalid.',
      message: 'Trading was requested, but this bot process has not started yet.',
      command: null,
      action: {
        label: 'Bind again',
        busyLabel: 'Binding...',
        runId: 'run-1',
        request,
      },
    });
    fixture.detectChanges();

    const button = fixture.nativeElement.querySelector(
      '[data-testid="sidebar-host-runner-action"]',
    ) as HTMLButtonElement | null;
    expect(fixture.nativeElement.textContent).toContain('Live binding invalid.');
    expect(button?.textContent?.trim()).toBe('Bind again');
    button?.click();
    await fixture.whenStable();

    expect(liveRuns.startHostRunner).toHaveBeenCalledWith('run-1', request);
  });

  it('keeps sidebar action in-flight state scoped to each notice instance', async () => {
    const { fixture, activeBotNotice, liveRuns } = setup();
    const request = bindAgainRequest();
    let finishFirst: () => void = () => undefined;
    liveRuns.startHostRunner
      .mockReturnValueOnce(new Promise<void>((resolve) => {
        finishFirst = resolve;
      }))
      .mockResolvedValueOnce(undefined);

    activeBotNotice.setNotice({
      instanceId: 'DEPVALJUL1',
      kind: 'live-binding-invalid',
      summary: 'Live binding invalid.',
      message: 'Trading was requested, but this bot process has not started yet.',
      command: null,
      action: {
        label: 'Bind again',
        busyLabel: 'Binding...',
        runId: 'run-1',
        request,
      },
    });
    fixture.detectChanges();

    const firstButton = fixture.nativeElement.querySelector(
      '[data-testid="sidebar-host-runner-action"]',
    ) as HTMLButtonElement;
    firstButton.click();
    await Promise.resolve();
    fixture.detectChanges();
    expect(firstButton.disabled).toBe(true);

    activeBotNotice.setNotice({
      instanceId: 'DEPVALJUL2',
      kind: 'live-binding-invalid',
      summary: 'Live binding invalid.',
      message: 'Trading was requested, but this bot process has not started yet.',
      command: null,
      action: {
        label: 'Bind again',
        busyLabel: 'Binding...',
        runId: 'run-2',
        request,
      },
    });
    fixture.detectChanges();

    const secondButton = fixture.nativeElement.querySelector(
      '[data-testid="sidebar-host-runner-action"]',
    ) as HTMLButtonElement;
    expect(secondButton.disabled).toBe(false);
    secondButton.click();
    await fixture.whenStable();

    expect(liveRuns.startHostRunner).toHaveBeenNthCalledWith(1, 'run-1', request);
    expect(liveRuns.startHostRunner).toHaveBeenNthCalledWith(2, 'run-2', request);
    finishFirst();
    await fixture.whenStable();
  });

  it('times out a hung invalid live-binding sidebar action and allows retry', async () => {
    vi.useFakeTimers();
    try {
      const { fixture, activeBotNotice, liveRuns } = setup();
      const request = bindAgainRequest();
      liveRuns.startHostRunner
        .mockReturnValueOnce(new Promise<void>(() => undefined))
        .mockResolvedValueOnce(undefined);
      activeBotNotice.setNotice({
        instanceId: 'DEPVALJUL1',
        kind: 'live-binding-invalid',
        summary: 'Live binding invalid.',
        message: 'Trading was requested, but this bot process has not started yet.',
        command: null,
        action: {
          label: 'Bind again',
          busyLabel: 'Binding...',
          runId: 'run-1',
          request,
        },
      });
      fixture.detectChanges();

      const button = fixture.nativeElement.querySelector(
        '[data-testid="sidebar-host-runner-action"]',
      ) as HTMLButtonElement;
      button.click();
      await Promise.resolve();
      fixture.detectChanges();
      expect(button.disabled).toBe(true);
      expect(button.textContent?.trim()).toBe('Binding...');

      await vi.advanceTimersByTimeAsync(15_000);
      fixture.detectChanges();

      const alert = fixture.nativeElement.querySelector('[role="alert"]') as HTMLElement | null;
      const retryButton = fixture.nativeElement.querySelector(
        '[data-testid="sidebar-host-runner-action"]',
      ) as HTMLButtonElement;
      expect(alert?.textContent).toContain('Timed out starting bot process. Try again.');
      expect(retryButton.disabled).toBe(false);
      expect(retryButton.textContent?.trim()).toBe('Bind again');

      retryButton.click();
      await Promise.resolve();
      expect(liveRuns.startHostRunner).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
