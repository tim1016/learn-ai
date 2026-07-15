import type { OperatorSurfaceEvidenceFact } from '../../../api/live-instances.types';
import { formatReceiptLabel, formatReceiptValue } from '../../../shared/pipes/receipt-label.pipe';
import { fmtTimestampNy } from '../format';

export interface DiagnosticEvidenceLine {
  readonly id: string;
  readonly message: string;
  readonly detail: string | null;
}

export function buildDiagnosticEvidenceLines(
  facts: readonly OperatorSurfaceEvidenceFact[],
): DiagnosticEvidenceLine[] {
  return facts.map((fact, index) => diagnosticEvidenceLine(fact, index));
}

function diagnosticEvidenceLine(
  fact: OperatorSurfaceEvidenceFact,
  index: number,
): DiagnosticEvidenceLine {
  return {
    id: `${fact.label}:${fact.source ?? 'unknown'}:${index}`,
    message: diagnosticEvidenceMessage(fact),
    detail: diagnosticEvidenceDetail(fact),
  };
}

function diagnosticEvidenceMessage(fact: OperatorSurfaceEvidenceFact): string {
  const value = formatReceiptValue(fact.label, fact.value);
  switch (fact.label) {
    case 'broker.connection':
      return `Broker connection is ${value.toLowerCase()}.`;
    case 'reconciliation.state':
      return `Reconciliation is ${value.toLowerCase()}.`;
    case 'account_clerk.generation':
      return `Account Clerk generation is ${value}.`;
    case 'account_clerk.lease_active':
      return `Account Clerk lease is ${value}.`;
    default:
      return `${formatReceiptLabel(fact.label)} is ${value}.`;
  }
}

function diagnosticEvidenceDetail(fact: OperatorSurfaceEvidenceFact): string | null {
  const parts = [
    fact.source ? `Source: ${formatReceiptLabel(fact.source)}` : null,
    fact.gate_id ? `Gate: ${formatReceiptLabel(fact.gate_id)}` : null,
    fact.ts_ms_resolved && fact.ts_ms !== null ? `Evidence time: ${fmtTimestampNy(fact.ts_ms)}` : null,
  ];
  const detail = parts.filter((part): part is string => part !== null).join('. ');
  return detail || null;
}
