import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { BrokerConnectivityStripComponent } from './broker-connectivity-strip.component';
import {
  BrokerConnectivityService,
  type ConnectivityLink,
  type DaemonFreshness,
} from '../../../services/broker-connectivity.service';

const UNKNOWN: DaemonFreshness = { state: 'unknown', sha: null, commitsBehind: null };

function renderStrip(
  links: ConnectivityLink[],
  blockers: string[] = [],
  freshness: DaemonFreshness = UNKNOWN,
) {
  const fake = {
    links: () => links,
    blockers: () => blockers,
    daemonDown: () => links.some((link) => link.key === 'daemon' && link.state === 'down'),
    daemonFreshness: () => freshness,
    reload: () => undefined,
  } as Partial<BrokerConnectivityService>;
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

  it('shows an "up to date" verdict when the running code matches the working tree', () => {
    const el = renderStrip(
      [
        { key: 'daemon', label: 'Live engine', state: 'ok', detail: 'Running' },
        { key: 'broker', label: 'Broker', state: 'ok', detail: 'Connected' },
        { key: 'fleet', label: 'Fleet policy', state: 'ok', detail: 'Clear' },
      ],
      [],
      { state: 'fresh', sha: 'a1b2c3d', commitsBehind: null },
    );

    expect(el.textContent).toContain('Engine code');
    expect(el.textContent).toContain('Up to date');
    expect(el.textContent).toContain('a1b2c3d');
  });

  it('flags stale code with a behind-count and a restart affordance', () => {
    const el = renderStrip(
      [
        { key: 'daemon', label: 'Live engine', state: 'ok', detail: 'Running' },
        { key: 'broker', label: 'Broker', state: 'ok', detail: 'Connected' },
        { key: 'fleet', label: 'Fleet policy', state: 'ok', detail: 'Clear' },
      ],
      [],
      { state: 'stale', sha: 'deadbee', commitsBehind: 3 },
    );

    expect(el.textContent).toContain('Stale');
    expect(el.textContent).toContain('3 commits behind');
    expect(el.textContent).toContain('restart to apply fixes');
    expect(el.textContent).toContain('Copy restart command');
  });

  it('omits the code line when freshness is unknown', () => {
    const el = renderStrip([
      { key: 'daemon', label: 'Live engine', state: 'ok', detail: 'Running' },
      { key: 'broker', label: 'Broker', state: 'ok', detail: 'Connected' },
      { key: 'fleet', label: 'Fleet policy', state: 'ok', detail: 'Clear' },
    ]);

    expect(el.textContent).not.toContain('Engine code');
  });

  it('distinguishes daemon-down from broker-down (not collapsed)', () => {
    const el = renderStrip(
      [
        { key: 'daemon', label: 'Live engine', state: 'down', detail: 'Unavailable' },
        { key: 'broker', label: 'Broker', state: 'ok', detail: 'Connected' },
        { key: 'fleet', label: 'Fleet policy', state: 'ok', detail: 'Clear' },
      ],
      ['Live engine unavailable — start it on this machine, then recheck.'],
    );

    expect(el.querySelector('.link.state-down')).toBeTruthy();
    expect(el.textContent).toContain('Live engine unavailable');
    expect(el.textContent).toContain('Copy start command');
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
