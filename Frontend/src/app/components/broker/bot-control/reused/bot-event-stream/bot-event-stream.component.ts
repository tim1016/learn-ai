import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  Injector,
  input,
  runInInjectionContext,
  signal,
} from '@angular/core';

import type {
  BotEventFactValue,
  BotEventRow,
  BotEventSeverity,
  GateStep,
} from '../../../../../api/live-runs.types';
import {
  formatReceiptLabel,
  formatReceiptValue,
  ReceiptLabelPipe,
} from '../../../../../shared/pipes/receipt-label.pipe';
import { formatLocalTimestamp } from '../../../../../utils/local-timestamp';
import { botEventRowStream, type BotEventRowStream } from './bot-event-row-stream';

interface FactEntry {
  readonly key: string;
  readonly label: string;
  readonly value: string;
}

interface DisplayRow {
  readonly row: BotEventRow;
  readonly localTime: string;
  readonly isoTime: string;
  readonly eventLabel: string;
  readonly severityLabel: string;
  readonly sourceLabel: string;
  readonly identity: readonly FactEntry[];
  readonly facts: readonly FactEntry[];
  readonly terminalFacts: readonly FactEntry[];
  readonly terminalExternalCode: string | null;
  readonly terminalCauseChain: string | null;
}

@Component({
  selector: 'app-bot-event-stream',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-event-stream.component.html',
  styleUrl: './bot-event-stream.component.scss',
})
export class BotEventStreamComponent {
  readonly runId = input.required<string>();

  private readonly injector = inject(Injector);
  private readonly expanded = signal<Set<number>>(new Set());
  private readonly stream = signal<BotEventRowStream | null>(null);

  constructor() {
    effect((onCleanup) => {
      const runId = this.runId();
      const next = runInInjectionContext(this.injector, () => botEventRowStream(runId));
      this.stream.set(next);
      onCleanup(() => next.close());
    });
  }

  readonly rows = computed<DisplayRow[]>(() =>
    (this.stream()?.rows() ?? []).map((row) => this.toDisplayRow(row)),
  );
  readonly isLoading = computed<boolean>(() =>
    this.stream()?.isLoading() ?? true,
  );
  readonly errorMessage = computed<string | null>(() => this.stream()?.errorMessage() ?? null);
  readonly rowCountLabel = computed<string>(() => `${this.rows().length} row(s)`);

  isExpanded(seq: number): boolean {
    return this.expanded().has(seq);
  }

  toggle(seq: number): void {
    this.expanded.update((current) => {
      const next = new Set(current);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  }

  severityClass(severity: BotEventSeverity): string {
    return `severity-${severity}`;
  }

  gateFacts(step: GateStep): readonly FactEntry[] {
    return factEntries(step.facts);
  }

  trackRow(_index: number, display: DisplayRow): number {
    return display.row.seq;
  }

  trackGate(index: number, step: GateStep): string {
    return `${step.evaluation_id}:${step.gate_id}:${step.gate_result}:${index}`;
  }

  trackFact(_index: number, entry: FactEntry): string {
    return entry.key;
  }

  private toDisplayRow(row: BotEventRow): DisplayRow {
    return {
      row,
      localTime: formatLocalTimestamp(row.ts_ms),
      isoTime: new Date(row.ts_ms).toISOString(),
      eventLabel: formatReceiptLabel(row.event_type),
      severityLabel: formatReceiptLabel(row.severity),
      sourceLabel: formatReceiptLabel(row.source_authority),
      identity: identityEntries(row),
      facts: factEntries(row.facts),
      terminalFacts: factEntries(row.terminal_error?.forensic_facts ?? {}),
      terminalExternalCode: terminalExternalCode(row),
      terminalCauseChain: row.terminal_error?.cause_chain.length
        ? row.terminal_error.cause_chain.join(' -> ')
        : null,
    };
  }
}

function terminalExternalCode(row: BotEventRow): string | null {
  const externalCode = row.terminal_error?.external_code;
  return externalCode === null || externalCode === undefined
    ? null
    : formatFactValue('external_code', externalCode);
}

function identityEntries(row: BotEventRow): readonly FactEntry[] {
  return factEntries({
    evaluation_id: row.identity.evaluation_id,
    intent_id: row.identity.intent_id,
    order_ref: row.identity.order_ref,
    req_id: row.identity.req_id,
    order_id: row.identity.order_id,
    perm_id: row.identity.perm_id,
    exec_id: row.identity.exec_id,
  }).filter((entry) => entry.value !== '-');
}

function factEntries(facts: Record<string, BotEventFactValue>): readonly FactEntry[] {
  return Object.entries(facts).map(([key, value]) => ({
    key,
    label: formatReceiptLabel(key),
    value: formatFactValue(key, value),
  }));
}

function formatFactValue(label: string, value: BotEventFactValue | undefined): string {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'string') return formatReceiptValue(label, value);
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}
