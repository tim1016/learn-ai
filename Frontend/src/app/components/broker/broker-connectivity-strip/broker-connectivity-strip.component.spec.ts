import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DaemonDiagnosticReport } from '../../../api/daemon-diagnostics.types';
import type { OperatorBlocker } from '../../../api/operator-blocker.types';
import { BrokerConnectivityStripComponent } from './broker-connectivity-strip.component';
import {
  BrokerConnectivityService,
  type ConnectivityLink,
  type DaemonFreshness,
} from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { operatorBlockerFixture } from '../../../testing/operator-blocker-fixtures';

const UNKNOWN: DaemonFreshness = { state: 'unknown', sha: null, commitsBehind: null };

function renderStrip(
  links: ConnectivityLink[],
  blockers: string[] = [],
  freshness: DaemonFreshness = UNKNOWN,
) {
  return setupStrip(links, blockers, freshness).el;
}

function setupStrip(
  links: ConnectivityLink[],
  blockers: string[] = [],
  freshness: DaemonFreshness = UNKNOWN,
  rosterBlockers: OperatorBlocker[] = [],
) {
  const fake = {
    links: () => links,
    blockers: () => blockers,
    rosterBlockers: () => rosterBlockers,
    daemonDown: () => links.some((link) => link.key === 'daemon' && link.state === 'down'),
    daemonFreshness: () => freshness,
    reload: () => undefined,
  } as Partial<BrokerConnectivityService>;
  const liveRuns = {
    getDaemonDiagnostics: vi.fn().mockResolvedValue(daemonReport()),
    renewControlPlaneLease: vi.fn().mockResolvedValue(null),
  };
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: BrokerConnectivityService, useValue: fake },
      { provide: LiveRunsService, useValue: liveRuns },
    ],
  });
  const fixture = TestBed.createComponent(BrokerConnectivityStripComponent);
  fixture.detectChanges();
  return { fixture, el: fixture.nativeElement as HTMLElement, liveRuns };
}

function daemonReport(): DaemonDiagnosticReport {
  return {
    overall_status: 'pass',
    transport: 'CONNECTED',
    dominant_condition: 'healthy',
    headline: {
      title: 'Live engine diagnostics are clear',
      summary: 'No daemon-control-plane fault was found in this snapshot.',
      remediation: null,
    },
    checks: [],
    per_instance: [],
    daemon_boot_id: 'boot-1',
    fetched_at_ms: 1_783_120_000_000,
  };
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

  it('renders backend-authored fleet roster blockers', () => {
    const el = setupStrip(
      [
        { key: 'daemon', label: 'Host daemon', state: 'ok', detail: 'Reachable' },
        { key: 'broker', label: 'Broker', state: 'ok', detail: 'Connected' },
        { key: 'fleet', label: 'Fleet policy', state: 'warn', detail: 'Contaminated — new starts blocked' },
      ],
      [],
      UNKNOWN,
      [
        operatorBlockerFixture({
          id: 'fleet_member_blocked',
          scope: 'fleet',
          host: 'fleet_roster',
          headline: 'bot-a is blocked',
          detail: 'Open the bot cockpit.',
          primaryMove: {
            label: 'Open bot cockpit',
            action: { kind: 'navigate', route: '/broker/bots/bot-a', fragment: null },
            target: null,
          },
        }),
      ],
    ).el;

    expect(el.querySelector('app-operator-blocker-list')).toBeTruthy();
    expect(el.textContent).toContain('bot-a is blocked');
    expect(el.textContent).toContain('Open bot cockpit');
  });

  it('renders the unknown (checking) state without a blocker alert', () => {
    const el = renderStrip([
      { key: 'daemon', label: 'Host daemon', state: 'unknown', detail: 'Checking…' },
      { key: 'broker', label: 'Broker', state: 'unknown', detail: 'Checking…' },
      { key: 'fleet', label: 'Fleet policy', state: 'unknown', detail: 'Checking…' },
    ]);

    expect(el.querySelector('[role="alert"]')).toBeNull();
  });

  it('loads the diagnostics report only when the Live engine link is opened', async () => {
    const { fixture, el, liveRuns } = setupStrip([
      { key: 'daemon', label: 'Live engine', state: 'ok', detail: 'Running' },
      { key: 'broker', label: 'Broker', state: 'ok', detail: 'Connected' },
      { key: 'fleet', label: 'Fleet policy', state: 'ok', detail: 'Clear' },
    ]);

    expect(liveRuns.getDaemonDiagnostics).not.toHaveBeenCalled();

    el.querySelector<HTMLButtonElement>('.link-button')?.click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(liveRuns.getDaemonDiagnostics).toHaveBeenCalledOnce();
    expect(el.textContent).toContain('Live engine diagnostics are clear');
  });
});
