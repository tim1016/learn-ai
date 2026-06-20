import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import type {
  ActionCapability,
  BrokerSafetyVerdict,
  LiveInstanceStatus,
  PriorRunClassification,
} from '../../../../api/live-instances.types';
import { getActionReasonCopy } from '../action-reason-codes';
import { deriveFleetState, type FleetState } from '../fleet-state';

type PillTone = 'running' | 'paused' | 'stopped' | 'stopping' | 'unknown';
type Verdict = 'paper' | 'unknown' | 'ready' | 'degraded' | 'blocked';
type PriorRun = 'success' | 'failure' | null;

/** Server-authored safety-verdict -> banner-pill tone map. */
const SAFETY_TONE: Record<BrokerSafetyVerdict, Verdict> = {
  PAPER: 'paper',
  LIVE: 'ready',
  DEGRADED: 'degraded',
  DISCONNECTED: 'blocked',
  UNKNOWN: 'unknown',
};

const SAFETY_LABEL: Record<BrokerSafetyVerdict, string> = {
  PAPER: 'PAPER-ONLY',
  LIVE: 'LIVE',
  DEGRADED: 'DEGRADED',
  DISCONNECTED: 'DISCONNECTED',
  UNKNOWN: 'UNKNOWN',
};

const PRIOR_RUN_FROM_PROJECTION: Record<PriorRunClassification, PriorRun> = {
  CLEAN: 'success',
  HALT_TRIGGERED: 'failure',
  EXITED_WITH_ERROR: 'failure',
  UNKNOWN: null,
};

const FLEET_VERDICT: Record<FleetState, 'ready' | 'degraded' | 'blocked'> = {
  STEADY: 'ready',
  CONFIGURE: 'degraded',
  BLOCKED: 'blocked',
};

/**
 * Sticky control bar — the persistent per-bot identity + status strip that
 * stays visible while the operator scrolls the long control panel.
 *
 * Terminal Cockpit visual identity (issue #591): renders a 3-column grid —
 * bot identity (name + strategy_instance_id sid), a centered pill cluster
 * (STATE / INTENT / SAFETY / LAST RUN + fleet-state verdict), and an action
 * toolbar with keycap-styled buttons. A 4-pixel attention strip sits along
 * the banner's bottom edge tinted by the fleet verdict (ready / degraded /
 * blocked), serving as a peripheral-vision indicator while the operator's
 * eyes are deep in the page.
 *
 * Command wiring stays where it is — this bar's `Jump to controls` keycap
 * scrolls the existing Start/Pause/Stop card into view rather than
 * duplicating destructive controls. Full keycap action rewire (PAUSE /
 * FLATTEN&PAUSE / kebab dialog) lands as a follow-up (slice #584) so that
 * sticky-banner UX and command-flow changes ship as separate, reviewable
 * diffs.
 */
@Component({
  selector: 'app-sticky-control-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './sticky-control-bar.component.html',
  styleUrl: './sticky-control-bar.component.scss',
})
export class StickyControlBarComponent {
  readonly status = input.required<LiveInstanceStatus>();
  /** Paper mode is conveyed by the account id (DU... prefix on IBKR) and
   * surfaced by the fleet header. The sticky bar receives it as an input
   * so it doesn't re-derive paper-vs-live from heuristics.
   *
   * NOTE — Slice 3 (#610): the SAFETY pill no longer reads ``isPaper()``;
   * it consumes ``operator_surface.broker.safety_verdict`` instead.  This
   * input is retained because the legacy template still references it,
   * and the parent (fleet header in #615) keeps using it for the
   * paper-reset visibility check.
   */
  readonly isPaper = input.required<boolean>();
  /** PRD #607 / Slice 3 (#610) — local request-in-flight flag composed
   * by the parent from its existing ``busyVerb`` signal.  Banner
   * keycaps disable when ``requestInFlight === true`` regardless of
   * server capability so a double-click does not fire two requests. */
  readonly requestInFlight = input<boolean>(false);

  readonly fleetState = computed<FleetState>(() => deriveFleetState(this.status()));

  readonly botName = computed<string>(() => this.status().strategy_instance_id);

  /** PRD #607 / Slice 3 (#610) — identity sub-line shows the strategy
   * key sourced from ``start_defaults.strategy``, falling back to the
   * strategy_instance_id when null. */
  readonly strategyKey = computed<string>(() => {
    const start = this.status().start_defaults;
    return (start?.strategy || '').trim() || this.status().strategy_instance_id;
  });

  readonly hasPoisonFlag = computed<boolean>(
    () => this.status().last_exit?.halt_trigger != null,
  );

  readonly processLabel = computed<string>(() => this.status().process.state);

  readonly processTone = computed<PillTone>(() => {
    switch (this.status().process.state) {
      case 'running':
        return 'running';
      case 'stopping':
        return 'stopping';
      case 'exited':
      case 'idle':
        return 'stopped';
      default:
        return 'unknown';
    }
  });

  readonly intentLabel = computed<string | null>(
    () => this.status().desired_state?.state ?? null,
  );

  readonly intentTone = computed<PillTone>(() => {
    switch (this.status().desired_state?.state) {
      case 'RUNNING':
        return 'running';
      case 'PAUSED':
        return 'paused';
      case 'STOPPED':
        return 'stopped';
      default:
        return 'unknown';
    }
  });

  /** PRD #607 / Slice 3 (#610) — safety pill consumes the
   * server-authored ``operator_surface.broker.safety_verdict`` enum
   * (PAPER / LIVE / DEGRADED / DISCONNECTED / UNKNOWN).  The Frontend
   * ``isPaper()`` derivation no longer drives this pill — the cockpit
   * renders what the server says rather than guessing from heuristics. */
  readonly safetyVerdict = computed<Verdict>(
    () => SAFETY_TONE[this.status().operator_surface.broker.safety_verdict],
  );

  readonly safetyLabel = computed<string>(
    () => SAFETY_LABEL[this.status().operator_surface.broker.safety_verdict],
  );

  /** PRD #607 / Slice 3 (#610) — prior-run pill consumes the
   * server-authored ``operator_surface.prior_run.classification``
   * enum instead of re-interpreting ``exit_code`` /``exit_reason`` /
   * ``halt_trigger`` in Angular. */
  readonly priorRun = computed<PriorRun>(
    () =>
      PRIOR_RUN_FROM_PROJECTION[
        this.status().operator_surface.prior_run.classification
      ],
  );

  /** Returns `null` when there is no prior run to report — callers must
   * hide the LAST RUN pill in that case. Defaulting to "CLEAN" on no-data
   * would be a false positive on a freshly deployed bot. */
  readonly priorRunLabel = computed<string | null>(() => {
    switch (this.priorRun()) {
      case 'failure':
        return 'LAST RUN FAULT';
      case 'success':
        return 'LAST RUN CLEAN';
      default:
        return null;
    }
  });

  /** Single mapping; both the FLEET pill's `data-verdict` and the banner's
   * `data-attention` attribute consume the same value — #591 review F1. */
  readonly fleetVerdict = computed<'ready' | 'degraded' | 'blocked'>(
    () => FLEET_VERDICT[this.fleetState()],
  );

  /** Emitted when the operator clicks "Jump to controls". The parent
   * scrolls the existing Start/Stop card into view; the sticky bar does
   * not own the controls themselves. */
  readonly jumpToControlsRequested = output();

  onJumpToControlsClick(): void {
    this.jumpToControlsRequested.emit(undefined);
  }

  // ─ Slice 3 keycap surface ────────────────────────────────────────
  //
  // Resume / Pause / Flatten-and-pause keycaps consume the
  // server-authored capabilities from operator_surface.actions.*.  The
  // parent listens to the emitted intent signals and routes them
  // through the existing LiveRunsService.setInstanceDesiredState and
  // the new LiveRunsService.flattenAndPause wrapper (the atomic Python
  // endpoint enforces PAUSE-before-FLATTEN; Angular MUST NOT
  // recompose this as issueCommand('FLATTEN') + setInstanceDesiredState).

  private readonly _actions = computed(() => this.status().operator_surface.actions);

  readonly resumeCapability = computed<ActionCapability>(() => this._actions().resume);
  readonly pauseCapability = computed<ActionCapability>(() => this._actions().pause);
  readonly flattenCapability = computed<ActionCapability>(
    () => this._actions().flatten_and_pause,
  );

  /** Resume label flips between ``Resume`` (live actuation will happen)
   * and ``Set intent: RUNNING`` (durable-only — host runner not bound
   * yet).  Server-driven via the ``effect`` discriminator from #608. */
  readonly resumeLabel = computed<string>(() =>
    this.resumeCapability().effect === 'LIVE_ACTUATION' ? 'Resume' : 'Set intent: RUNNING',
  );

  readonly pauseLabel = computed<string>(() =>
    this.pauseCapability().effect === 'LIVE_ACTUATION' ? 'Pause' : 'Set intent: PAUSED',
  );

  /** Keycap disabled state composes server capability AND local
   * ``requestInFlight``; the server reason-code vocabulary deliberately
   * excludes ``BUSY_VERB_IN_FLIGHT`` (Angular concern). */
  readonly resumeDisabled = computed<boolean>(
    () => !this.resumeCapability().enabled || this.requestInFlight(),
  );
  readonly pauseDisabled = computed<boolean>(
    () => !this.pauseCapability().enabled || this.requestInFlight(),
  );
  readonly flattenDisabled = computed<boolean>(
    () => !this.flattenCapability().enabled || this.requestInFlight(),
  );

  readonly resumeTooltip = computed<string>(() => this._tooltip(this.resumeCapability()));
  readonly pauseTooltip = computed<string>(() => this._tooltip(this.pauseCapability()));
  readonly flattenTooltip = computed<string>(() => this._tooltip(this.flattenCapability()));

  private _tooltip(cap: ActionCapability): string {
    if (!cap.enabled) {
      return getActionReasonCopy(cap.disabled_reason_code);
    }
    if (this.requestInFlight()) {
      return 'Request in flight — please wait';
    }
    return '';
  }

  readonly resumeRequested = output();
  readonly pauseRequested = output();
  readonly flattenAndPauseRequested = output();

  onResumeClick(): void {
    if (this.resumeDisabled()) return;
    this.resumeRequested.emit(undefined);
  }
  onPauseClick(): void {
    if (this.pauseDisabled()) return;
    this.pauseRequested.emit(undefined);
  }
  onFlattenClick(): void {
    if (this.flattenDisabled()) return;
    this.flattenAndPauseRequested.emit(undefined);
  }
}
