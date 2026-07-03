import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type {
  AccountTruthExecutionRow,
  AccountTruthFactOwner,
} from '../../../api/broker-models';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { fmtCurrency, fmtNumber, fmtTimestampLocal } from '../format';

interface ExecutionHistoryRow {
  execution: AccountTruthExecutionRow;
  timeMs: number;
  uncertaintyCodes: string[];
}

interface ExecutionHistoryDay {
  key: string;
  label: string;
  rows: ExecutionHistoryRow[];
}

interface ExecutionHistoryGroup {
  ownerKey: string;
  ownerLabel: string;
  owner: AccountTruthFactOwner;
  executionCount: number;
  uncertaintyCount: number;
  days: ExecutionHistoryDay[];
}

const LOCAL_DAY_FORMATTER = new Intl.DateTimeFormat('en-US', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

@Component({
  selector: 'app-account-truth-execution-history',
  imports: [ReceiptLabelPipe],
  templateUrl: './account-truth-execution-history.component.html',
  styleUrl: './account-truth-execution-history.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AccountTruthExecutionHistoryComponent {
  readonly executions = input.required<AccountTruthExecutionRow[]>();

  readonly executionGroups = computed<ExecutionHistoryGroup[]>(() =>
    this.groupExecutions(this.executions()),
  );

  readonly executionCount = computed(() => this.executions().length);

  readonly fmtCurrency = fmtCurrency;
  readonly fmtNumber = fmtNumber;
  readonly fmtTimestampLocal = fmtTimestampLocal;

  trackGroup = (_: number, group: ExecutionHistoryGroup): string => group.ownerKey;
  trackDay = (_: number, day: ExecutionHistoryDay): string => day.key;
  trackExecution = (_: number, row: ExecutionHistoryRow): string =>
    `${row.execution.exec_id}:${row.execution.observed_at_ms}`;
  trackUncertainty = (_: number, code: string): string => code;

  private groupExecutions(
    executions: AccountTruthExecutionRow[],
  ): ExecutionHistoryGroup[] {
    const groups = new Map<string, ExecutionHistoryRow[]>();
    for (const execution of executions) {
      const key = this.ownerKey(execution.owner);
      const row: ExecutionHistoryRow = {
        execution,
        timeMs: execution.exec_time_ms ?? execution.observed_at_ms,
        uncertaintyCodes: execution.uncertainty_codes,
      };
      groups.set(key, [...(groups.get(key) ?? []), row]);
    }

    return [...groups.entries()]
      .map(([ownerKey, rows]) => this.toGroup(ownerKey, rows))
      .sort(
        (a, b) =>
          a.ownerLabel.localeCompare(b.ownerLabel) ||
          a.ownerKey.localeCompare(b.ownerKey),
      );
  }

  private toGroup(ownerKey: string, rows: ExecutionHistoryRow[]): ExecutionHistoryGroup {
    const owner = rows[0].execution.owner;
    const days = new Map<string, ExecutionHistoryRow[]>();
    for (const row of rows) {
      const dayKey = this.dayKey(row.timeMs);
      days.set(dayKey, [...(days.get(dayKey) ?? []), row]);
    }

    return {
      ownerKey,
      ownerLabel: owner.owner_label,
      owner,
      executionCount: rows.length,
      uncertaintyCount: rows.filter((row) => row.uncertaintyCodes.length > 0).length,
      days: [...days.entries()]
        .map(([key, dayRows]) => ({
          key,
          label: key,
          rows: [...dayRows].sort((a, b) => b.timeMs - a.timeMs),
        }))
        .sort((a, b) => b.key.localeCompare(a.key)),
    };
  }

  private ownerKey(owner: AccountTruthFactOwner): string {
    return [
      owner.owner_class,
      owner.owner_key,
      owner.evidence_tier,
      owner.owner_binding_state,
    ].join(':');
  }

  private dayKey(ms: number): string {
    const parts = LOCAL_DAY_FORMATTER.formatToParts(new Date(ms));
    const year = parts.find((part) => part.type === 'year')?.value ?? '0000';
    const month = parts.find((part) => part.type === 'month')?.value ?? '00';
    const day = parts.find((part) => part.type === 'day')?.value ?? '00';
    return `${year}-${month}-${day}`;
  }

}
