import { ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { describe, expect, it, vi } from 'vitest';
import { BrokerBannerComponent } from './broker-banner.component';
import { BrokerHealthService } from '../services/broker-health.service';
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

function setup() {
  const brokerHealth = new FakeBrokerHealthService();
  const activeBotNotice = new ActiveBotSidebarNoticeService();
  TestBed.configureTestingModule({
    imports: [BrokerBannerComponent],
    providers: [
      { provide: BrokerHealthService, useValue: brokerHealth },
      { provide: ActiveBotSidebarNoticeService, useValue: activeBotNotice },
    ],
  });
  const fixture = TestBed.createComponent(BrokerBannerComponent);
  fixture.detectChanges();
  return { fixture, brokerHealth, activeBotNotice };
}

function toggle(fixture: ComponentFixture<BrokerBannerComponent>): HTMLButtonElement | null {
  return fixture.nativeElement.querySelector('.broker-toggle') as HTMLButtonElement | null;
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

  it('renders active bot host-runner warning above the IBKR banner', () => {
    const { fixture, brokerHealth, activeBotNotice } = setup();
    activeBotNotice.setNotice({
      instanceId: 'DEPVAL-DIA-20260626',
      message: 'The bot service is offline.',
      command: 'make host-runner',
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
});
