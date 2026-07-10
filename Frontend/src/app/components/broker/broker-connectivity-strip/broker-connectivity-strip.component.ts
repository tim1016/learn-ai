import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { Router } from '@angular/router';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { DaemonDiagnosticsStore } from '../../../services/daemon-diagnostics-store.service';
import { DaemonDiagnosticsPanelComponent } from '../daemon-diagnostics/daemon-diagnostics-panel.component';
import {
  OperatorBlockerListComponent,
  type OperatorBlockerMoveEvent,
} from '../shared/operator-blocker-list/operator-blocker-list.component';

/**
 * Shared connectivity strip (handoff: cross-cuts broker pages). Renders the
 * three plumbing signals — host daemon, broker, fleet policy — that previously
 * collapsed into one fuzzy "unreachable/empty" state, so an operator can tell
 * daemon-down from broker-down from policy-block at a glance. Per-control
 * disable-with-reason reads the same `BrokerConnectivityService`.
 */
@Component({
  selector: 'app-broker-connectivity-strip',
  imports: [DaemonDiagnosticsPanelComponent, OperatorBlockerListComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'closeDiagnosticsIfOpen()',
  },
  templateUrl: './broker-connectivity-strip.component.html',
  styleUrl: './broker-connectivity-strip.component.scss',
})
export class BrokerConnectivityStripComponent {
  protected readonly connectivity = inject(BrokerConnectivityService);
  protected readonly diagnostics = inject(DaemonDiagnosticsStore);
  private readonly router = inject(Router);
  private readonly diagnosticsTrigger = viewChild<ElementRef<HTMLButtonElement>>('diagnosticsTrigger');
  private readonly diagnosticsDialog = viewChild<ElementRef<HTMLElement>>('diagnosticsDialog');
  protected readonly copied = signal<boolean>(false);
  protected readonly diagnosticsOpen = signal<boolean>(false);
  protected readonly startCommand =
    'PYTHONPATH=PythonDataService PythonDataService/.venv/bin/python -m app.engine.live.host_daemon --repo-root .';
  // Restarting a STALE daemon must first kill the running one — it still owns the
  // port, so a bare start would fail with "address in use". (The down case uses
  // startCommand: nothing to kill.)
  protected readonly restartCommand = `pkill -f "app.engine.live.host_daemon"; ${this.startCommand}`;

  protected recheck(): void {
    this.connectivity.reload();
  }

  protected async openDiagnostics(): Promise<void> {
    this.diagnosticsOpen.set(true);
    queueMicrotask(() => this.diagnosticsDialog()?.nativeElement.focus());
    if (this.diagnostics.report() === null) {
      await this.refreshDiagnostics();
    }
  }

  protected closeDiagnostics(): void {
    if (!this.diagnosticsOpen()) return;
    this.diagnosticsOpen.set(false);
    queueMicrotask(() => this.diagnosticsTrigger()?.nativeElement.focus());
  }

  protected closeDiagnosticsIfOpen(): void {
    if (this.diagnosticsOpen()) {
      this.closeDiagnostics();
    }
  }

  protected async refreshDiagnostics(): Promise<void> {
    await this.diagnostics.refresh();
  }

  protected async renewLeaseFromDiagnostics(): Promise<void> {
    await this.diagnostics.renewLease();
    this.connectivity.reload();
  }

  protected exportDiagnostics(): void {
    const report = this.diagnostics.report();
    if (report === null || typeof document === 'undefined') return;
    const blob = new Blob(
      [JSON.stringify({ note: 'Paths and sensitive fields were redacted before export.', report }, null, 2)],
      { type: 'application/json' },
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `daemon-diagnostics-${report.fetched_at_ms}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  protected async navigateFromDiagnostics(path: string): Promise<void> {
    this.closeDiagnostics();
    await this.router.navigateByUrl(path);
  }

  protected async handleRosterBlockerMove(event: OperatorBlockerMoveEvent): Promise<void> {
    const action = event.move.action;
    if (action.kind === 'navigate') {
      await this.router.navigateByUrl(action.fragment ? `${action.route}#${action.fragment}` : action.route);
    }
  }

  protected async copyStartCommand(): Promise<void> {
    await this._copy(this.startCommand);
  }

  protected async copyRestartCommand(): Promise<void> {
    await this._copy(this.restartCommand);
  }

  private async _copy(text: string): Promise<void> {
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    await navigator.clipboard.writeText(text);
    this.copied.set(true);
  }
}
