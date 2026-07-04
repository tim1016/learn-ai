import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  DaemonDiagnosticAction,
  DaemonDiagnosticCategory,
  DaemonDiagnosticCheck,
  DaemonDiagnosticReport,
  DaemonDiagnosticStatus,
  DaemonReportStatus,
} from '../../../api/daemon-diagnostics.types';

interface CheckGroup {
  label: string;
  checks: DaemonDiagnosticCheck[];
}

const CATEGORY_LABELS: Record<DaemonDiagnosticCategory, string> = {
  reachability: 'Reachability',
  auth: 'Authentication',
  contract: 'Contract',
  code_freshness: 'Code',
  lease: 'Lease',
  boot: 'Boot',
  process_registry: 'Process registry',
  orphans: 'Orphans',
  socket_probe: 'Socket probe',
  process: 'Process',
  sockets: 'Sockets',
  runtime_freshness: 'Runtime',
  artifacts: 'Artifacts',
};

@Component({
  selector: 'app-daemon-diagnostics-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './daemon-diagnostics-panel.component.html',
  styleUrl: './daemon-diagnostics-panel.component.scss',
})
export class DaemonDiagnosticsPanelComponent {
  readonly report = input<DaemonDiagnosticReport | null>(null);
  readonly loading = input<boolean>(false);
  readonly error = input<string | null>(null);
  readonly compact = input<boolean>(false);
  readonly controls = input<boolean>(true);

  readonly refresh = output();
  readonly renewLease = output();
  readonly exportReport = output();
  readonly navigateTo = output<string>();

  protected readonly globalGroups = computed<CheckGroup[]>(() =>
    groupChecks(this.report()?.checks ?? []),
  );
  protected readonly instanceReports = computed(
    () => this.report()?.per_instance ?? [],
  );

  protected refreshClicked(): void {
    this.refresh.emit();
  }

  protected exportClicked(): void {
    this.exportReport.emit();
  }

  protected runAction(action: DaemonDiagnosticAction): void {
    if (action.kind === 'recovery_mutation' && action.action_id === 'renew_lease') {
      this.renewLease.emit();
      return;
    }
    if (action.kind === 'navigation' && action.deep_link) {
      this.navigateTo.emit(action.deep_link);
    }
  }

  protected runCheckAction(check: DaemonDiagnosticCheck): void {
    if (this.actionIsRunnable(check.action)) {
      this.runAction(check.action);
    }
  }

  protected actionLabel(check: DaemonDiagnosticCheck): string {
    return this.actionIsRunnable(check.action) ? check.action.label : '';
  }

  protected actionIsRunnable(action: DaemonDiagnosticAction | null): action is DaemonDiagnosticAction {
    return action !== null && (
      (action.kind === 'recovery_mutation' && action.action_id === 'renew_lease') ||
      (action.kind === 'navigation' && action.deep_link !== null)
    );
  }

  protected formatTimestamp(ms: number): string {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'medium',
    }).format(new Date(ms));
  }

  protected statusClass(value: { status?: DaemonDiagnosticStatus; overall_status?: DaemonReportStatus }): string {
    return `status-${value.status ?? value.overall_status ?? 'skip'}`;
  }

  protected groupTrack(_index: number, group: CheckGroup): string {
    return group.label;
  }

  protected checkTrack(_index: number, check: DaemonDiagnosticCheck): string {
    return check.check_id;
  }
}

function groupChecks(checks: DaemonDiagnosticCheck[]): CheckGroup[] {
  const out = new Map<DaemonDiagnosticCategory, DaemonDiagnosticCheck[]>();
  for (const check of checks) {
    const existing = out.get(check.category) ?? [];
    existing.push(check);
    out.set(check.category, existing);
  }
  return Array.from(out.entries()).map(([category, items]) => ({
    label: CATEGORY_LABELS[category],
    checks: items,
  }));
}
