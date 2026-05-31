import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { BrokerConnectivityStripComponent } from './broker-connectivity-strip.component';
import {
  BrokerConnectivityService,
  type ConnectivityLink,
} from '../../../services/broker-connectivity.service';

function renderStrip(links: ConnectivityLink[], blockers: string[] = []) {
  const fake = { links: () => links, blockers: () => blockers } as Partial<BrokerConnectivityService>;
  TestBed.configureTestingModule({
    providers: [{ provide: BrokerConnectivityService, useValue: fake }],
  });
  const fixture = TestBed.createComponent(BrokerConnectivityStripComponent);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('BrokerConnectivityStripComponent', () => {
  it('renders all three links with their detail text', () => {
    const el = renderStrip([
      { key: 'daemon', label: 'Host daemon', state: 'ok', detail: 'Reachable' },
      { key: 'broker', label: 'Broker', state: 'ok', detail: 'Connected' },
      { key: 'fleet', label: 'Fleet policy', state: 'ok', detail: 'Clear' },
    ]);

    expect(el.textContent).toContain('Host daemon');
    expect(el.textContent).toContain('Reachable');
    expect(el.textContent).toContain('Connected');
    expect(el.textContent).toContain('Clear');
  });

  it('distinguishes daemon-down from broker-down (not collapsed)', () => {
    const el = renderStrip(
      [
        { key: 'daemon', label: 'Host daemon', state: 'down', detail: 'Unreachable — start the host daemon process' },
        { key: 'broker', label: 'Broker', state: 'ok', detail: 'Connected' },
        { key: 'fleet', label: 'Fleet policy', state: 'ok', detail: 'Clear' },
      ],
      ['Host daemon unreachable — start the host daemon to deploy or control runs.'],
    );

    expect(el.querySelector('.link.state-down')).toBeTruthy();
    expect(el.textContent).toContain('Unreachable');
    expect(el.textContent).toContain('Connected');
  });

  it('shows blocker reasons as an alert when plumbing is down', () => {
    const el = renderStrip(
      [
        { key: 'daemon', label: 'Host daemon', state: 'ok', detail: 'Reachable' },
        { key: 'broker', label: 'Broker', state: 'down', detail: 'Disconnected' },
        { key: 'fleet', label: 'Fleet policy', state: 'warn', detail: 'Contaminated — new starts blocked' },
      ],
      [
        'Broker disconnected — connect IBKR to act on a live run.',
        'Fleet policy is blocking new starts (account contaminated).',
      ],
    );

    const alert = el.querySelector('[role="alert"]');
    expect(alert).toBeTruthy();
    expect(alert?.textContent).toContain('Broker disconnected');
    expect(alert?.textContent).toContain('Fleet policy is blocking');
  });

  it('renders the unknown (checking) state without a blocker alert', () => {
    const el = renderStrip([
      { key: 'daemon', label: 'Host daemon', state: 'unknown', detail: 'Checking…' },
      { key: 'broker', label: 'Broker', state: 'unknown', detail: 'Checking…' },
      { key: 'fleet', label: 'Fleet policy', state: 'unknown', detail: 'Checking…' },
    ]);

    expect(el.querySelector('[role="alert"]')).toBeNull();
  });
});
