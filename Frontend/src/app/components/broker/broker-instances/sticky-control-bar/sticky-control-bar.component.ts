import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import type { LiveInstanceStatus } from '../../../../api/live-instances.types';
import { deriveFleetState, type FleetState } from '../fleet-state';

type PillTone = 'running' | 'paused' | 'stopped' | 'stopping' | 'unknown';
type Verdict = 'paper' | 'unsafe' | 'unknown' | 'ready' | 'degraded' | 'blocked';
type PriorRun = 'success' | 'failure' | null;
type Attention = 'ready' | 'degraded' | 'blocked';

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
   * so it doesn't re-derive paper-vs-live from heuristics. */
  readonly isPaper = input.required<boolean>();

  readonly fleetState = computed<FleetState>(() => deriveFleetState(this.status()));

  readonly botName = computed<string>(() => this.status().strategy_instance_id);

  readonly hasPoisonFlag = computed<boolean>(() => {
    const trigger = this.status().last_exit?.halt_trigger;
    return trigger !== null && trigger !== undefined;
  });

  readonly processLabel = computed<string>(() => {
    const s = this.status().process.state;
    return s.toUpperCase();
  });

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

  readonly intentLabel = computed<string | null>(() => {
    const intent = this.status().desired_state?.state;
    return intent ? intent : null;
  });

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

  readonly safetyVerdict = computed<Verdict>(() => {
    if (this.hasPoisonFlag()) return 'unsafe';
    return this.isPaper() ? 'paper' : 'unknown';
  });

  readonly safetyLabel = computed<string>(() => {
    if (this.hasPoisonFlag()) return 'UNSAFE';
    return this.isPaper() ? 'PAPER-ONLY' : 'LIVE';
  });

  readonly priorRun = computed<PriorRun>(() => {
    const exit = this.status().last_exit;
    if (!exit) return null;
    if (exit.halt_trigger !== null && exit.halt_trigger !== undefined) return 'failure';
    if (exit.exit_code === 0 || exit.exit_reason === 'normal') return 'success';
    if (exit.exit_code !== null && exit.exit_code !== 0) return 'failure';
    return null;
  });

  readonly priorRunLabel = computed<string>(() =>
    this.priorRun() === 'failure' ? 'LAST RUN FAULT' : 'LAST RUN CLEAN',
  );

  readonly fleetVerdict = computed<Verdict>(() => {
    switch (this.fleetState()) {
      case 'STEADY':
        return 'ready';
      case 'CONFIGURE':
        return 'degraded';
      case 'BLOCKED':
        return 'blocked';
      default:
        return 'unknown';
    }
  });

  readonly attentionState = computed<Attention>(() => {
    switch (this.fleetState()) {
      case 'STEADY':
        return 'ready';
      case 'CONFIGURE':
        return 'degraded';
      default:
        return 'blocked';
    }
  });

  /** Emitted when the operator clicks "Jump to controls". The parent
   * scrolls the existing Start/Stop card into view; the sticky bar does
   * not own the controls themselves. */
  readonly jumpToControlsRequested = output();

  onJumpToControlsClick(): void {
    this.jumpToControlsRequested.emit(undefined);
  }
}
