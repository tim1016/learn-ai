import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';

import type {
  HostProcessStartDisabledReasonCode,
  HostProcessState,
  OperatorSurfaceHostProcess,
} from '../../../../../api/live-instances.types';
import { LiveRunsService } from '../../../../../services/live-runs.service';

const HEADING_BY_STATE: Record<HostProcessState, string> = {
  RUNNING: '',
  STOPPING: 'HOST PROCESS STOPPING',
  EXITED: 'HOST PROCESS EXITED',
  IDLE: 'HOST RUNNER IDLE',
  WAITING_FOR_HOST: 'WAITING FOR HOST PROCESS',
  UNREACHABLE: 'HOST RUNNER UNREACHABLE',
};

/** Trader-facing copy for each closed `HostProcessStartDisabledReasonCode`.
 *  Per ADR 0013 §4, Angular maps closed server-authored enums to display
 *  strings; the server is authoritative for the enum value. */
const START_DISABLED_COPY: Record<HostProcessStartDisabledReasonCode, string> = {
  ALREADY_RUNNING: 'The bot is already running.',
  STOPPING: 'The bot is shutting down. Wait for it to finish before starting again.',
  HOST_SERVICE_OFFLINE:
    'The bot service is offline. Start it on the host machine first, then try again.',
  STOPPED_REQUIRES_REDEPLOY:
    'This run is permanently stopped. Redeploy the bot to trade again.',
  START_SETTINGS_INCOMPLETE:
    "This bot's saved start settings are incomplete. Review Configuration and redeploy.",
};

/**
 * Server-authored host-process notice (PRD #607 / Slice 8 / #615;
 * Phase 0 trader-language redesign 2026-06-22).
 *
 * Renders the cockpit-side surface that replaced the legacy
 * ``<app-broker-start-stop-card>`` (removed in the 2026-06-22 cockpit
 * audit; the component had no live references and was already
 * documented as superseded). When ``host_process.state !== 'RUNNING'``
 * the notice surfaces:
 *   - the server-authored ``notice`` line verbatim;
 *   - (only when present) a server-authored ``copyable_command`` block
 *     — currently emitted by the projection only for UNREACHABLE, when
 *     trusted deployment configuration supplies the host-service start
 *     command (ADR 0013 amendment 2026-06-22);
 *   - the per-instance Start bot process button, driven by the
 *     server-authored ``start_capability`` (ADR-0006 §1 / ADR-0007).
 *
 * Angular MUST NOT construct, interpolate, transform, or assemble any
 * command string or start-request body — those are rendered verbatim
 * from the server. Disabled-button copy is the only Angular-owned
 * mapping (closed-enum lookup, permitted by ADR 0013 §4).
 *
 * No REDEPLOY link lives here; REDEPLOY is a separate surface for
 * creating a new run configuration.
 */
@Component({
  selector: 'app-host-process-notice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './host-process-notice.component.html',
  styleUrl: './host-process-notice.component.scss',
})
export class HostProcessNoticeComponent {
  private readonly liveRuns = inject(LiveRunsService);

  readonly hostProcess = input.required<OperatorSurfaceHostProcess>();
  /** Server-authored desired-state label (RUNNING / PAUSED / STOPPED)
   * sourced from the existing ``desired_state.state`` field — the
   * cockpit surfaces what the host runner will do on its next start. */
  readonly desiredIntent = input<string | null>(null);

  readonly visible = computed<boolean>(() => this.hostProcess().state !== 'RUNNING');

  readonly heading = computed<string>(() => HEADING_BY_STATE[this.hostProcess().state]);

  readonly notice = computed<string | null>(() => this.hostProcess().notice);

  readonly copyableCommand = computed<string | null>(
    () => this.hostProcess().copyable_command,
  );

  readonly startCapability = computed(() => this.hostProcess().start_capability);

  readonly startDisabledMessage = computed<string | null>(() => {
    const code = this.startCapability().disabled_reason_code;
    return code ? START_DISABLED_COPY[code] : null;
  });

  readonly startInFlight = signal(false);
  readonly startError = signal<string | null>(null);

  copyCommand(): void {
    const cmd = this.copyableCommand();
    if (cmd && typeof navigator !== 'undefined' && navigator.clipboard) {
      void navigator.clipboard.writeText(cmd);
    }
  }

  async startBotProcess(): Promise<void> {
    const cap = this.startCapability();
    if (!cap.enabled || !cap.run_id || !cap.request || this.startInFlight()) {
      return;
    }
    this.startInFlight.set(true);
    this.startError.set(null);
    try {
      await this.liveRuns.startHostRunner(cap.run_id, cap.request);
      // The next poll surfaces the new RUNNING state; no manual refresh.
    } catch (err: unknown) {
      this.startError.set(this._formatStartError(err));
    } finally {
      this.startInFlight.set(false);
    }
  }

  private _formatStartError(err: unknown): string {
    if (err instanceof HttpErrorResponse) {
      const detail = (err.error as { detail?: string } | null)?.detail;
      return detail || err.message || 'Failed to start bot process.';
    }
    if (err instanceof Error) {
      return err.message;
    }
    return 'Failed to start bot process.';
  }
}
