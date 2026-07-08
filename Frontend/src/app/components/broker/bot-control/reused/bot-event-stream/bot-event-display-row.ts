import type {
  BotEventFactValue,
  BotEventRow,
  GateStep,
} from '../../../../../api/live-runs.types';
import {
  formatReceiptLabel,
  formatReceiptValue,
} from '../../../../../shared/pipes/receipt-label.pipe';
import { formatLocalTimestamp } from '../../../../../utils/local-timestamp';

export interface FactEntry {
  readonly key: string;
  readonly label: string;
  readonly value: string;
}

export interface DisplayRow {
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

export function toDisplayRow(row: BotEventRow): DisplayRow {
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

export function gateFacts(step: GateStep): readonly FactEntry[] {
  return factEntries(step.facts);
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
