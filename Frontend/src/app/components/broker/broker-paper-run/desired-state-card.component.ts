import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from '@angular/core';
import type {
  DesiredState,
  DesiredStateAction,
  RunState,
} from '../../../api/live-runs.types';
import { fmtTimestampNy } from '../format';

/**
 * UI-2 + UI-3 — Desired-state (durable intent) card.
 *
 * UI-2 renders the durable operator intent distinctly from RUN state and
 * PROCESS state, labelling its `path_status` explicitly so an
 * `unknown_no_ledger_binding` is never silently rendered as RUNNING.
 *
 * UI-3 adds Pause / Resume / Stop controls backed by the desired-state
 * write API. A `corrupt` sidecar blocks the controls with a clear error;
 * controls are hidden when `path_status = unknown_no_ledger_binding`.
 *
 * This component owns no data fetching — it receives the resolved
 * `DesiredState` from the parent and emits an action for the parent to
 * write + reload. `busy`/`error` are driven by the parent's write flow.
 */
@Component({
  selector: 'app-desired-state-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './desired-state-card.component.html',
  styleUrl: './desired-state-card.component.scss',
})
export class DesiredStateCardComponent {
  readonly desired = input.required<DesiredState>();
  readonly strategyInstanceId = input<string | null>(null);
  readonly runState = input<RunState | null>(null);
  readonly busy = input<boolean>(false);
  readonly writeError = input<string | null>(null);

  readonly act = output<DesiredStateAction>();

  /** Inline confirm guard for STOP (a destructive durable transition). */
  readonly confirmingStop = signal<boolean>(false);

  readonly fmtTimestampNy = fmtTimestampNy;

  readonly pathStatus = computed(() => this.desired().path_status);

  /** No ledger binding → we cannot locate the sidecar; hide all controls. */
  readonly noLedgerBinding = computed<boolean>(
    () => this.pathStatus() === 'unknown_no_ledger_binding',
  );

  /** Corrupt sidecar → controls disabled and an explicit error shown. */
  readonly corrupt = computed<boolean>(() => this.pathStatus() === 'corrupt');

  /** Effective durable intent; `absent` defaults to the engine RUNNING. */
  readonly effectiveState = computed<string>(() => {
    const d = this.desired();
    if (d.path_status === 'unknown_no_ledger_binding') return 'UNKNOWN';
    if (d.path_status === 'corrupt') return 'CORRUPT';
    if (d.path_status === 'absent') return 'RUNNING';
    return d.state ?? 'RUNNING';
  });

  /** Source/derivation label for the effective state. */
  readonly stateProvenance = computed<string>(() => {
    switch (this.pathStatus()) {
      case 'ok':
        return 'desired-state sidecar';
      case 'absent':
        return 'engine default (no sidecar written yet)';
      case 'corrupt':
        return 'desired-state sidecar unreadable';
      case 'unknown_no_ledger_binding':
        return 'no run → strategy-instance binding in ledger';
    }
  });

  readonly statusBadgeClass = computed<string>(() => {
    switch (this.effectiveState()) {
      case 'RUNNING':
        return 'badge-running';
      case 'PAUSED':
        return 'badge-paused';
      case 'STOPPED':
      case 'CORRUPT':
        return 'badge-stopped';
      default:
        return 'badge-unknown';
    }
  });

  /** Controls are available only when we can address a real sidecar. */
  readonly controlsEnabled = computed<boolean>(
    () => !this.noLedgerBinding() && !this.corrupt(),
  );

  readonly canPause = computed<boolean>(
    () => this.controlsEnabled() && !this.busy() && this.effectiveState() !== 'PAUSED',
  );

  readonly canResume = computed<boolean>(
    () =>
      this.controlsEnabled()
      && !this.busy()
      && this.effectiveState() !== 'RUNNING',
  );

  readonly canStop = computed<boolean>(
    () => this.controlsEnabled() && !this.busy() && this.effectiveState() !== 'STOPPED',
  );

  pause(): void {
    if (this.canPause()) this.act.emit('pause');
  }

  resume(): void {
    if (this.canResume()) this.act.emit('resume');
  }

  requestStop(): void {
    if (this.canStop()) this.confirmingStop.set(true);
  }

  cancelStop(): void {
    this.confirmingStop.set(false);
  }

  confirmStop(): void {
    if (!this.canStop()) return;
    this.confirmingStop.set(false);
    this.act.emit('stop');
  }
}
