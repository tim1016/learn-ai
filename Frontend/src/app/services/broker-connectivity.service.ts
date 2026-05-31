import { Injectable, computed, inject, resource } from '@angular/core';
import { BrokerHealthService } from './broker-health.service';
import { LiveRunsService } from './live-runs.service';

export type LinkState = 'ok' | 'down' | 'warn' | 'unknown';

export interface ConnectivityLink {
  key: 'daemon' | 'broker' | 'fleet';
  label: string;
  state: LinkState;
  detail: string;
}

/**
 * Single source of truth for broker "is the plumbing up?" (handoff: shared
 * connectivity strip). Composes the three signals that today collapse into one
 * fuzzy state — host daemon reachability, broker connection, and fleet
 * contamination policy — so both the strip AND per-control disable-with-reason
 * read the same derivation rather than each re-deriving it.
 */
@Injectable({ providedIn: 'root' })
export class BrokerConnectivityService {
  private readonly svc = inject(LiveRunsService);
  private readonly brokerHealth = inject(BrokerHealthService);

  /** Host-daemon health — the only proof the subprocess bridge is reachable. */
  readonly daemon = resource({ loader: () => this.svc.getHostRunnerHealth() });
  /** Fleet contamination + policy (ADR 0005). */
  readonly fleet = resource({ loader: () => this.svc.getAccountFleet() });

  readonly daemonState = computed<LinkState>(() => {
    if (this.daemon.isLoading()) return 'unknown';
    if (this.daemon.error()) return 'down';
    return this.daemon.value()?.ok ? 'ok' : 'down';
  });

  readonly brokerState = computed<LinkState>(() => {
    const h = this.brokerHealth.health();
    if (h === null) return 'unknown';
    return h.connected ? 'ok' : 'down';
  });

  readonly fleetState = computed<LinkState>(() => {
    const f = this.fleet.value();
    if (this.fleet.isLoading() || f === undefined) return 'unknown';
    if (f.verdict === 'contaminated' && f.policy_blocks_starts) return 'warn';
    return 'ok';
  });

  readonly links = computed<ConnectivityLink[]>(() => [
    {
      key: 'daemon',
      label: 'Host daemon',
      state: this.daemonState(),
      detail:
        this.daemonState() === 'ok'
          ? 'Reachable'
          : this.daemonState() === 'unknown'
            ? 'Checking…'
            : 'Unreachable — start the host daemon process',
    },
    {
      key: 'broker',
      label: 'Broker',
      state: this.brokerState(),
      detail:
        this.brokerState() === 'ok'
          ? 'Connected'
          : this.brokerState() === 'unknown'
            ? 'Checking…'
            : 'Disconnected',
    },
    {
      key: 'fleet',
      label: 'Fleet policy',
      state: this.fleetState(),
      detail:
        this.fleetState() === 'warn'
          ? 'Contaminated — new starts blocked'
          : this.fleetState() === 'unknown'
            ? 'Checking…'
            : 'Clear',
    },
  ]);

  /**
   * Human "why is this blocked?" reasons for whichever plumbing is down. The
   * console's disable-with-reason renders these so a disabled control is never
   * a bare greyed button.
   */
  readonly blockers = computed<string[]>(() => {
    const out: string[] = [];
    if (this.daemonState() === 'down') {
      out.push('Host daemon unreachable — start the host daemon to deploy or control runs.');
    }
    if (this.brokerState() === 'down') {
      out.push('Broker disconnected — connect IBKR to act on a live run.');
    }
    if (this.fleetState() === 'warn') {
      out.push('Fleet policy is blocking new starts (account contaminated).');
    }
    return out;
  });

  readonly daemonReachable = computed<boolean>(() => this.daemonState() === 'ok');
  /** Explicitly down (probe failed) — distinct from 'unknown' while loading, so
   * disable-with-reason doesn't block a control mid-probe (#416). */
  readonly daemonDown = computed<boolean>(() => this.daemonState() === 'down');
  readonly fleetBlocksStarts = computed<boolean>(() => this.fleetState() === 'warn');

  reload(): void {
    this.daemon.reload();
    this.fleet.reload();
    void this.brokerHealth.refresh();
  }
}
