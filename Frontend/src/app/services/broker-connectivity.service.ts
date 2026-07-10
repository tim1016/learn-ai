import { DestroyRef, Injectable, computed, inject, resource, signal } from '@angular/core';
import type { FleetRosterRow, FleetRosterSnapshot } from '../api/live-instances.types';
import {
  openFleetRosterStream,
  type FleetRosterStream,
} from './fleet-roster-stream';
import { adoptVersionedSnapshot } from './versioned-snapshot-stream';
import { BrokerHealthService } from './broker-health.service';
import { LiveRunsService } from './live-runs.service';
import type { OperatorBlocker } from '../api/operator-blocker.types';

export type LinkState = 'ok' | 'down' | 'warn' | 'unknown';

/** Structured broker connection state from the backend (auto-reconnect
 * hardening). The strip and per-control disable-with-reason both derive
 * their LinkState from this. */
export type BrokerConnectionState =
  | 'connected'
  | 'soft_lost'
  | 'subscriptions_stale'
  | 'degraded_data_farm'
  | 'reconnecting'
  | 'recovering'
  | 'hard_down'
  | 'disconnected'
  | 'disabled';

export interface ConnectivityLink {
  key: 'daemon' | 'broker' | 'fleet';
  label: string;
  state: LinkState;
  detail: string;
}

/** Whether the host daemon is running the latest code. `unknown` while loading
 * or when git is unavailable; `stale` means the working tree is ahead of the
 * running process and it must be restarted to apply merged fixes. */
export interface DaemonFreshness {
  state: 'fresh' | 'stale' | 'unknown';
  sha: string | null;
  commitsBehind: number | null;
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
  private readonly destroyRef = inject(DestroyRef);
  private readonly fleetRosterSnapshot = signal<FleetRosterSnapshot | null>(null);
  private readonly rosterStreamState = signal<'closed' | 'connecting' | 'open' | 'error'>('closed');
  private readonly supportsFleetRosterStream = typeof EventSource !== 'undefined';
  private fleetRosterStream: FleetRosterStream | null = null;

  /** Host-daemon health — the only proof the subprocess bridge is reachable. */
  readonly daemon = resource({ loader: () => this.svc.getHostRunnerHealth() });
  /** Fleet contamination + policy (ADR 0005). */
  readonly fleet = resource({ loader: () => this.svc.getAccountFleet() });
  /** REST roster fallback for runtimes without EventSource. Browsers use the
   * fleet roster stream as the single live roster source. */
  readonly instances = resource<readonly FleetRosterRow[] | undefined, unknown>({
    loader: () => {
      if (this.supportsFleetRosterStream) return Promise.resolve(undefined);
      return this.svc.getInstances();
    },
  });
  readonly rosterInstances = computed<readonly FleetRosterRow[] | undefined>(
    () => this.fleetRosterSnapshot()?.instances ?? this.instances.value(),
  );
  readonly fleetRosterStatus = this.rosterStreamState.asReadonly();

  /** No strategy instances exist at all — distinct from a clean/contaminated
   * account. Loaded (not undefined) and empty. */
  readonly nothingDeployed = computed<boolean>(() => {
    const list = this.rosterInstances();
    return list !== undefined && list.length === 0;
  });

  readonly rosterBlockers = computed<OperatorBlocker[]>(() =>
    (this.rosterInstances() ?? []).flatMap((row) => row.blockers ?? []),
  );

  constructor() {
    this.openFleetRoster();
    this.destroyRef.onDestroy(() => this.fleetRosterStream?.close());
  }

  readonly daemonState = computed<LinkState>(() => {
    if (this.daemon.isLoading()) return 'unknown';
    if (this.daemon.error()) return 'down';
    return this.daemon.value()?.ok ? 'ok' : 'down';
  });

  /** The structured connection state from the backend. ``null`` while the
   * first health probe is in flight; otherwise the backend's
   * ``connection_state`` (required field — frontend and backend deploy
   * together so the partial-rollout fallback would only ever paper over
   * a misconfigured deploy). */
  readonly brokerConnectionState = computed<BrokerConnectionState | null>(() => {
    const h = this.brokerHealth.health();
    if (h === null) return null;
    return (h.connection_state ?? null) as BrokerConnectionState | null;
  });

  /** Single source of truth for the link colour and detail string per
   * connection state. Co-located so the strip can't drift between the
   * dot's amber and the label's "Reconnecting". */
  private static readonly STATE_RENDERING: Record<
    BrokerConnectionState,
    { link: LinkState; baseDetail: string }
  > = {
    connected: { link: 'ok', baseDetail: 'Connected' },
    reconnecting: { link: 'warn', baseDetail: 'Reconnecting' },
    recovering: { link: 'warn', baseDetail: 'Recovering streams' },
    soft_lost: { link: 'warn', baseDetail: 'Connection degraded — feed lost, recovering' },
    subscriptions_stale: { link: 'warn', baseDetail: 'Subscriptions stale — resubscribe required' },
    degraded_data_farm: { link: 'warn', baseDetail: 'IBKR data farm degraded' },
    hard_down: { link: 'down', baseDetail: 'Recovery exhausted' },
    disabled: { link: 'unknown', baseDetail: 'Disabled' },
    disconnected: { link: 'down', baseDetail: 'Disconnected' },
  };

  readonly brokerState = computed<LinkState>(() => {
    const state = this.brokerConnectionState();
    if (state === null) return 'unknown';
    return BrokerConnectivityService.STATE_RENDERING[state].link;
  });

  /** Operator-facing detail string for the broker link. ``Reconnecting``
   * surfaces the attempt counter when the backend publishes it so the
   * operator sees progress rather than silence; ``disabled`` surfaces
   * the backend-authored ``reason`` so the operator sees why. */
  readonly brokerDetail = computed<string>(() => {
    const state = this.brokerConnectionState();
    if (state === null) return 'Checking…';
    const { baseDetail } = BrokerConnectivityService.STATE_RENDERING[state];
    const h = this.brokerHealth.health();
    const condition = h?.condition ?? null;
    if (state === 'reconnecting' && h?.reconnect_attempt) {
      return `${condition?.title ?? baseDetail} (attempt ${h.reconnect_attempt})`;
    }
    if (state === 'recovering') {
      return h?.recovery_error ? `Recovery failed: ${h.recovery_error}` : baseDetail;
    }
    if (state === 'subscriptions_stale' && h?.last_ibkr_code) {
      return `${baseDetail} (${h.last_ibkr_code})`;
    }
    if (state === 'degraded_data_farm' && h?.last_ibkr_code) {
      return `${baseDetail} (${h.last_ibkr_code})`;
    }
    if (state === 'disabled' && h?.reason) {
      return h.reason;
    }
    return condition?.title ?? baseDetail;
  });

  /** Whether the connected session is the paper account. Paper-only UI
   * surfaces (Reset Paper Account, foreign-exec-replay warnings) gate on
   * this. ``null`` when the health probe hasn't returned yet — callers
   * should treat null as "unknown, don't render the paper-only thing
   * yet" rather than substituting a default. */
  isPaper(): boolean | null {
    const h = this.brokerHealth.health();
    if (h === null) return null;
    if (!h.connected) return null;
    return h.is_paper === true;
  }

  readonly fleetState = computed<LinkState>(() => {
    if (
      this.fleet.isLoading() ||
      (this.instances.isLoading() && this.fleetRosterSnapshot() === null)
    ) {
      return 'unknown';
    }
    // Nothing deployed reads as neutral (grey), not a healthy "Clear" green —
    // the detail text "Nothing deployed" carries the distinction (WCAG: not
    // colour-alone).
    if (this.nothingDeployed()) return 'unknown';
    const f = this.fleet.value();
    if (f === undefined) return 'unknown';
    if (f.verdict === 'contaminated' && f.policy_blocks_starts) return 'warn';
    return 'ok';
  });

  readonly links = computed<ConnectivityLink[]>(() => [
    {
      key: 'daemon',
      label: 'Live engine',
      state: this.daemonState(),
      detail:
        this.daemonState() === 'ok'
          ? 'Running'
          : this.daemonState() === 'unknown'
            ? 'Checking…'
            : 'Unavailable',
    },
    {
      key: 'broker',
      label: 'Broker',
      state: this.brokerState(),
      detail: this.brokerDetail(),
    },
    {
      key: 'fleet',
      label: 'Fleet policy',
      state: this.fleetState(),
      detail: this.nothingDeployed()
        ? 'Nothing deployed'
        : this.fleetState() === 'warn'
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
      out.push('Live engine unavailable — start it on this machine, then recheck.');
    }
    const bs = this.brokerConnectionState();
    const condition = this.brokerHealth.health()?.condition ?? null;
    const brokerConditionBlocker = condition?.remediation
      ? `${condition.summary} ${condition.remediation}`
      : condition?.summary;
    if (brokerConditionBlocker !== undefined && bs !== 'connected' && bs !== 'disabled') {
      out.push(brokerConditionBlocker);
    } else if (bs === 'disconnected') {
      out.push('Broker disconnected — connect IBKR to act on a live run.');
    } else if (bs === 'reconnecting') {
      out.push('Broker reconnecting — order entry paused until the link is restored.');
    } else if (bs === 'recovering') {
      out.push('Broker recovering streams — order entry paused until subscriptions and probes pass.');
    } else if (bs === 'hard_down') {
      out.push('Broker recovery exhausted — manual reconnect or Gateway intervention required.');
    } else if (bs === 'soft_lost') {
      out.push('Broker feed lost — auto-recovery in progress. Hold off on order entry.');
    } else if (bs === 'subscriptions_stale') {
      out.push('Broker subscriptions are stale after reconnect — resubscribe streams before order entry.');
    } else if (bs === 'degraded_data_farm') {
      out.push('IBKR data farm is degraded — market data may be stale or incomplete.');
    }
    if (this.fleetState() === 'warn') {
      out.push('Fleet policy is blocking new starts (account contaminated).');
    }
    return out;
  });

  /** Verdict on whether the host daemon is running the latest code, so the
   * operator gets an answer ("up to date" / "restart to apply") instead of a
   * bare hash to eyeball. The daemon does NOT reload on `git pull`: the engine
   * reports the SHA it is RUNNING (git_sha) and the on-disk HEAD; when they
   * differ it is stale and must be restarted (#449). */
  readonly daemonFreshness = computed<DaemonFreshness>(() => {
    const h = this.daemon.value();
    const sha = h?.git_sha ? h.git_sha.slice(0, 7) : null;
    // A daemon that supports the freshness contract ALWAYS reports repo_head_sha.
    // A legacy (pre-change) daemon sends only git_sha — computed from the on-disk
    // HEAD — which is exactly the misleading value this feature exists to catch
    // (old daemon still running after a pull). So when repo_head_sha is absent we
    // must read 'unknown', never 'fresh'. ('unknown' WITH a sha = legacy daemon;
    // the strip prompts a restart to a current build.)
    if (!h || !h.git_sha || h.repo_head_sha === null || h.repo_head_sha === undefined) {
      return { state: 'unknown', sha, commitsBehind: null };
    }
    return {
      state: h.code_stale ? 'stale' : 'fresh',
      sha,
      commitsBehind: h.commits_behind ?? null,
    };
  });

  readonly daemonReachable = computed<boolean>(() => this.daemonState() === 'ok');
  /** Explicitly down (probe failed) — distinct from 'unknown' while loading, so
   * disable-with-reason doesn't block a control mid-probe (#416). */
  readonly daemonDown = computed<boolean>(() => this.daemonState() === 'down');
  readonly fleetBlocksStarts = computed<boolean>(() => this.fleetState() === 'warn');

  reload(): void {
    this.daemon.reload();
    this.fleet.reload();
    this.instances.reload();
    this.fleetRosterSnapshot.set(null);
    this.openFleetRoster();
    void this.brokerHealth.refresh();
  }

  private openFleetRoster(): void {
    this.fleetRosterStream?.close();
    this.fleetRosterStream = null;
    if (!this.supportsFleetRosterStream) {
      this.rosterStreamState.set('closed');
      return;
    }
    this.fleetRosterStream = openFleetRosterStream({
      onStatus: (status) => this.rosterStreamState.set(status),
      onMalformedSnapshot: () => this.rosterStreamState.set('error'),
      onSnapshot: (candidate) => {
        this.rosterStreamState.set('open');
        this.fleetRosterSnapshot.update((current) =>
          adoptVersionedSnapshot(current, candidate),
        );
      },
    });
  }
}
