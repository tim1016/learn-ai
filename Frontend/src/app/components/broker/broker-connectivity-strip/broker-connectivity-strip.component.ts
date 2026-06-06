import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';

/**
 * Shared connectivity strip (handoff: cross-cuts broker pages). Renders the
 * three plumbing signals — host daemon, broker, fleet policy — that previously
 * collapsed into one fuzzy "unreachable/empty" state, so an operator can tell
 * daemon-down from broker-down from policy-block at a glance. Per-control
 * disable-with-reason reads the same `BrokerConnectivityService`.
 */
@Component({
  selector: 'app-broker-connectivity-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './broker-connectivity-strip.component.html',
  styleUrl: './broker-connectivity-strip.component.scss',
})
export class BrokerConnectivityStripComponent {
  protected readonly connectivity = inject(BrokerConnectivityService);
  protected readonly copied = signal<boolean>(false);
  protected readonly startCommand =
    'PYTHONPATH=PythonDataService PythonDataService/.venv/bin/python -m app.engine.live.host_daemon --repo-root .';
  // Restarting a STALE daemon must first kill the running one — it still owns the
  // port, so a bare start would fail with "address in use". (The down case uses
  // startCommand: nothing to kill.)
  protected readonly restartCommand = `pkill -f "app.engine.live.host_daemon"; ${this.startCommand}`;

  protected recheck(): void {
    this.connectivity.reload();
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
