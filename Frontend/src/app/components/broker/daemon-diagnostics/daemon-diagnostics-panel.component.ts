import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  input,
  output,
  signal,
  untracked,
} from '@angular/core';
import {
  Accordion,
  AccordionContent,
  AccordionHeader,
  AccordionPanel,
} from 'primeng/accordion';

import type {
  DaemonDiagnosticAction,
  DaemonDiagnosticCategory,
  DaemonDiagnosticCheck,
  DaemonDiagnosticReport,
  DaemonDiagnosticStatus,
  DaemonReportStatus,
} from '../../../api/daemon-diagnostics.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

interface CheckGroup {
  value: DaemonDiagnosticCategory;
  label: string;
  checks: DaemonDiagnosticCheck[];
  status: DaemonDiagnosticStatus;
}

type AccordionValue = string | number | string[] | number[] | null | undefined;

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
  imports: [
    Accordion,
    AccordionContent,
    AccordionHeader,
    AccordionPanel,
    ReceiptLabelPipe,
  ],
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
  readonly globalCheckAccordionValue = signal<string[]>([]);

  protected readonly globalGroups = computed<CheckGroup[]>(() =>
    groupChecks(this.report()?.checks ?? []),
  );
  protected readonly instanceReports = computed(
    () => this.report()?.per_instance ?? [],
  );

  constructor() {
    effect(() => {
      const openGroups = this.globalGroups()
        .filter((group) => groupRequiresAttention(group))
        .map((group) => group.value);
      untracked(() => this.globalCheckAccordionValue.set(openGroups));
    });
  }

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

  protected formatCount(value: number, singular: string): string {
    return `${value} ${singular}${value === 1 ? '' : 's'}`;
  }

  protected statusClass(value: { status?: DaemonDiagnosticStatus; overall_status?: DaemonReportStatus }): string {
    return `status-${value.status ?? value.overall_status ?? 'skip'}`;
  }

  protected statusLabel(status: DaemonDiagnosticStatus | DaemonReportStatus): string {
    switch (status) {
      case 'pass':
        return 'Clear';
      case 'warn':
        return 'Needs attention';
      case 'fail':
        return 'Error';
      case 'skip':
        return 'Skipped';
    }
  }

  protected statusIcon(status: DaemonDiagnosticStatus | DaemonReportStatus): string {
    switch (status) {
      case 'pass':
        return 'pi pi-check-circle';
      case 'warn':
        return 'pi pi-exclamation-triangle';
      case 'fail':
        return 'pi pi-times-circle';
      case 'skip':
        return 'pi pi-minus-circle';
    }
  }

  protected groupSummary(group: CheckGroup): string {
    const nonPassCount = group.checks.filter((check) => check.status !== 'pass').length;
    if (nonPassCount === 0) {
      return `${this.formatCount(group.checks.length, 'check')} clear`;
    }
    const verb = nonPassCount === 1 ? 'needs' : 'need';
    return `${this.formatCount(nonPassCount, 'check')} ${verb} attention`;
  }

  protected globalCheckPanelOpen(group: CheckGroup): boolean {
    return this.globalCheckAccordionValue().includes(group.value);
  }

  protected setGlobalCheckAccordionValue(value: AccordionValue): void {
    this.globalCheckAccordionValue.set(coerceAccordionValue(value));
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
    value: category,
    label: CATEGORY_LABELS[category],
    checks: items,
    status: statusForChecks(items),
  }));
}

function statusForChecks(checks: readonly DaemonDiagnosticCheck[]): DaemonDiagnosticStatus {
  if (checks.some((check) => check.status === 'fail')) return 'fail';
  if (checks.some((check) => check.status === 'warn')) return 'warn';
  if (checks.some((check) => check.status === 'skip')) return 'skip';
  return 'pass';
}

function groupRequiresAttention(group: CheckGroup): boolean {
  return group.status === 'warn' || group.status === 'fail';
}

function coerceAccordionValue(value: AccordionValue): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item));
  if (value === null || value === undefined) return [];
  return [String(value)];
}
