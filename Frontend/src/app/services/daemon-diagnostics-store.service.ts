import { Injectable, inject, signal } from '@angular/core';

import type { DaemonDiagnosticReport } from '../api/daemon-diagnostics.types';
import { LiveRunsService } from './live-runs.service';

@Injectable({ providedIn: 'root' })
export class DaemonDiagnosticsStore {
  private readonly liveRuns = inject(LiveRunsService);
  private readonly _report = signal<DaemonDiagnosticReport | null>(null);
  private readonly _loading = signal<boolean>(false);
  private readonly _error = signal<string | null>(null);

  readonly report = this._report.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  async refresh(): Promise<void> {
    this._loading.set(true);
    this._error.set(null);
    try {
      this._report.set(await this.liveRuns.getDaemonDiagnostics());
    } catch (error) {
      this._error.set(humanError(error));
    } finally {
      this._loading.set(false);
    }
  }

  async renewLease(): Promise<void> {
    this._loading.set(true);
    this._error.set(null);
    try {
      await this.liveRuns.renewControlPlaneLease();
      this._report.set(await this.liveRuns.getDaemonDiagnostics());
    } catch (error) {
      this._error.set(humanError(error));
    } finally {
      this._loading.set(false);
    }
  }
}

function humanError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return 'Live engine diagnostics could not be loaded.';
}
