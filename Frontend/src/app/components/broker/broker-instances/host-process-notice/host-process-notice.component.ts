import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type {
  HostProcessState,
  OperatorSurfaceHostProcess,
} from '../../../../api/live-instances.types';

const HEADING_BY_STATE: Record<HostProcessState, string> = {
  RUNNING: '',
  STOPPED: 'HOST PROCESS STOPPED',
  CRASHED: 'HOST PROCESS CRASHED',
  STARTING: 'HOST PROCESS STARTING',
  UNKNOWN: 'HOST PROCESS STATE UNKNOWN',
};

/**
 * Server-authored host-process notice (PRD #607 / Slice 8 / #615).
 *
 * Renders the cockpit-side surface that replaces the deleted
 * ``<app-broker-start-stop-card>`` — the host runner is operator-owned
 * (ADR-0003 / ADR-0007) so the cockpit cannot start / stop it.  When
 * ``host_process.state !== 'RUNNING'`` the notice surfaces the
 * server-authored ``notice`` line verbatim plus (only when present) a
 * server-authored ``copyable_command`` block.
 *
 * Angular MUST NOT construct, interpolate, transform, or assemble any
 * command — it renders the string verbatim or omits the row entirely.
 * No REDEPLOY link and no "restart" affordance live here; REDEPLOY is
 * a separate surface for creating a new run configuration.
 */
@Component({
  selector: 'app-host-process-notice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './host-process-notice.component.html',
  styleUrl: './host-process-notice.component.scss',
})
export class HostProcessNoticeComponent {
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

  copyCommand(): void {
    const cmd = this.copyableCommand();
    if (cmd && typeof navigator !== 'undefined' && navigator.clipboard) {
      void navigator.clipboard.writeText(cmd);
    }
  }
}
