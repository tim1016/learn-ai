import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { FleetHeaderComponent } from './fleet-header.component';
import {
  BrokerConnectivityService,
  type ConnectivityLink,
  type DaemonFreshness,
} from '../../../../services/broker-connectivity.service';
import type { FleetContamination } from '../../../../api/live-instances.types';
import type { OperationError } from '../../operation-error';

interface RenderInput {
  account?: FleetContamination | null;
  selectedInstanceId?: string | null;
  busyEmergencyFlatten?: boolean;
  commandError?: OperationError | null;
  freshness?: DaemonFreshness;
  links?: ConnectivityLink[];
  blockers?: string[];
}

const UNKNOWN: DaemonFreshness = { state: 'unknown', sha: null, commitsBehind: null };
const NEUTRAL_LINKS: ConnectivityLink[] = [
  { key: 'daemon', label: 'Live engine', state: 'ok', detail: 'Running' },
  { key: 'broker', label: 'Broker', state: 'ok', detail: 'Connected' },
  { key: 'fleet', label: 'Fleet policy', state: 'ok', detail: 'Clear' },
];

function cleanAccount(): FleetContamination {
  return {
    net_positions: null,
    explained_total: {},
    explained_by_instance: [],
    residual: {},
    verdict: 'clean',
    policy_blocks_starts: false,
    summary: 'Account is clean.',
  };
}

function contaminatedAccount(): FleetContamination {
  return {
    net_positions: { AAPL: 100 },
    explained_total: {},
    explained_by_instance: [],
    residual: { AAPL: 100 },
    verdict: 'contaminated',
    policy_blocks_starts: true,
    summary: 'Unrecognized AAPL position detected.',
  };
}

function render(opts: RenderInput = {}) {
  const fakeConnectivity = {
    links: () => opts.links ?? NEUTRAL_LINKS,
    blockers: () => opts.blockers ?? [],
    daemonDown: () =>
      (opts.links ?? NEUTRAL_LINKS).some((l) => l.key === 'daemon' && l.state === 'down'),
    daemonFreshness: () => opts.freshness ?? UNKNOWN,
    isPaper: () => true,
    brokerState: () => 'ok' as const,
    reload: () => undefined,
  } as Partial<BrokerConnectivityService>;

  TestBed.configureTestingModule({
    providers: [{ provide: BrokerConnectivityService, useValue: fakeConnectivity }],
  });
  const fixture = TestBed.createComponent(FleetHeaderComponent);
  fixture.componentRef.setInput('account', opts.account ?? null);
  fixture.componentRef.setInput('selectedInstanceId', opts.selectedInstanceId ?? null);
  fixture.componentRef.setInput('busyEmergencyFlatten', opts.busyEmergencyFlatten ?? false);
  fixture.componentRef.setInput('commandError', opts.commandError ?? null);
  fixture.detectChanges();
  return fixture;
}

afterEach(() => TestBed.resetTestingModule());

describe('FleetHeaderComponent', () => {
  describe('Account Status card', () => {
    it('renders the clean-account badge and reassuring copy', () => {
      const fixture = render({ account: cleanAccount() });
      const el = fixture.nativeElement as HTMLElement;

      expect(el.textContent).toContain('Account Status');
      expect(el.textContent).toContain('ALL POSITIONS ACCOUNTED FOR');
      expect(el.textContent).toContain(
        'Every open position in your account is managed by a known strategy.',
      );
      expect(el.querySelector('.account-card.problem')).toBeNull();
      expect(el.querySelector('.status-chip.ok')).toBeTruthy();
    });

    it('renders the contaminated-account badge with residual positions and a problem outline', () => {
      const fixture = render({ account: contaminatedAccount() });
      const el = fixture.nativeElement as HTMLElement;

      expect(el.textContent).toContain('UNRECOGNIZED POSITIONS DETECTED');
      expect(el.textContent).toContain('Unrecognized AAPL position detected.');
      expect(el.querySelector('.account-card.problem')).toBeTruthy();
      expect(el.querySelector('.status-chip.bad')).toBeTruthy();
      const pills = el.querySelectorAll('.position-pills li');
      expect(pills).toHaveLength(1);
      expect(pills[0].textContent).toContain('AAPL');
      expect(pills[0].textContent).toContain('100');
    });

    it('renders the unknown-account warn badge', () => {
      const acct: FleetContamination = {
        net_positions: null,
        explained_total: {},
        explained_by_instance: [],
        residual: {},
        verdict: 'unknown',
        policy_blocks_starts: false,
        summary: "Couldn't verify account state.",
      };
      const fixture = render({ account: acct });
      const el = fixture.nativeElement as HTMLElement;

      expect(el.textContent).toContain('ACCOUNT STATUS UNKNOWN');
      expect(el.querySelector('.status-chip.warn')).toBeTruthy();
    });

    it('hides the Account Status card when account input is null', () => {
      const fixture = render({ account: null });
      const el = fixture.nativeElement as HTMLElement;

      expect(el.querySelector('.account-card')).toBeNull();
    });
  });

  describe('Platform Update banner', () => {
    it('renders when daemon code is stale', () => {
      const fixture = render({
        freshness: { state: 'stale', sha: 'deadbee', commitsBehind: 2 },
      });
      const el = fixture.nativeElement as HTMLElement;

      const banner = el.querySelector('[data-testid="platform-update-banner"]');
      expect(banner).toBeTruthy();
      expect(banner?.textContent).toContain('Platform update available');
    });

    it('does not render when daemon code is fresh', () => {
      const fixture = render({
        freshness: { state: 'fresh', sha: 'a1b2c3d', commitsBehind: null },
      });
      const el = fixture.nativeElement as HTMLElement;

      expect(el.querySelector('[data-testid="platform-update-banner"]')).toBeNull();
    });

    it('does not render when daemon freshness is unknown', () => {
      const fixture = render({ freshness: UNKNOWN });
      const el = fixture.nativeElement as HTMLElement;

      expect(el.querySelector('[data-testid="platform-update-banner"]')).toBeNull();
    });
  });

  describe('Account Safety Actions disclosure', () => {
    it('hosts Emergency Flatten and disables it when no bot is selected', () => {
      const fixture = render({ account: cleanAccount(), selectedInstanceId: null });
      const el = fixture.nativeElement as HTMLElement;

      const button = el.querySelector<HTMLButtonElement>('.emergency-actions button.danger');
      expect(button).toBeTruthy();
      expect(button?.disabled).toBe(true);
      expect(el.textContent).toContain('Select a bot above to enable the account-wide flatten');
    });

    it('enables Emergency Flatten and emits the request event when clicked', () => {
      const fixture = render({ account: cleanAccount(), selectedInstanceId: 'bot-a' });
      const el = fixture.nativeElement as HTMLElement;

      let requested = 0;
      fixture.componentInstance.emergencyFlattenRequested.subscribe(() => requested++);

      const button = el.querySelector<HTMLButtonElement>('.emergency-actions button.danger');
      expect(button?.disabled).toBe(false);
      button?.click();
      fixture.detectChanges();

      expect(requested).toBe(1);
    });

    it('shows the in-flight label while busy', () => {
      const fixture = render({
        account: cleanAccount(),
        selectedInstanceId: 'bot-a',
        busyEmergencyFlatten: true,
      });
      const el = fixture.nativeElement as HTMLElement;

      const button = el.querySelector<HTMLButtonElement>('.emergency-actions button.danger');
      expect(button?.textContent).toContain('Flattening');
      expect(button?.disabled).toBe(true);
    });
  });
});
