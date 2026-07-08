import type {
  ExposureCoherenceConfirmation,
  ExposureCoherencePosture,
  IdentityCoherenceConfirmation,
} from '../../../api/live-runs.types';
import { formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import { exposurePositionsLabel, normalizedSymbol } from '../lib/deploy-prefill-params';

export interface IdentitySymbolEvidence {
  label: string;
  value: string;
  source: string;
}

export interface IdentityCoherenceConflict {
  summary: string;
  signature: string;
  facts: IdentitySymbolEvidence[];
}

export interface ExposureCoherenceConflict {
  posture: ExposureCoherencePosture;
  pendingOrderCount: number | null;
  ownedPositions: Record<string, number>;
  positionsLabel: string;
  source: string;
  summary: string;
  signature: string;
}

export interface CoherenceConfirmationCardFact {
  label: string;
  value: string;
  source?: string;
  valueReceiptLabel?: boolean;
  sourceReceiptLabel?: boolean;
}

export function identityCoherenceCardFacts(
  evidence: IdentityCoherenceConflict | null,
): CoherenceConfirmationCardFact[] {
  return (
    evidence?.facts.map((fact) => ({
      label: fact.label,
      value: fact.value,
      source: fact.source,
      sourceReceiptLabel: true,
    })) ?? []
  );
}

export function exposureCoherenceCardFacts(
  evidence: ExposureCoherenceConflict | null,
): CoherenceConfirmationCardFact[] {
  if (evidence === null) return [];
  return [
    { label: 'Posture', value: evidence.posture, valueReceiptLabel: true },
    { label: 'Positions', value: evidence.positionsLabel },
    {
      label: 'Pending orders',
      value: evidence.pendingOrderCount === null ? 'unknown' : String(evidence.pendingOrderCount),
    },
    { label: 'Source', value: evidence.source, valueReceiptLabel: true },
  ];
}

export function buildIdentityCoherenceEvidence(input: {
  inheritedSymbol: string;
  inheritedSymbolSource: string;
  signalStream: string;
  actionPlanSymbol: string | null;
}): IdentityCoherenceConflict | null {
  const inherited = normalizedSymbol(input.inheritedSymbol);
  if (!inherited) return null;
  const facts: IdentitySymbolEvidence[] = [
    {
      label: 'Inherited bot symbol',
      value: inherited,
      source: input.inheritedSymbolSource.trim() || 'request inherited symbol',
    },
  ];
  if (input.signalStream) {
    facts.push({
      label: 'Signal stream',
      value: input.signalStream,
      source: 'live_config.symbol',
    });
  }
  if (input.actionPlanSymbol) {
    facts.push({
      label: 'Action plan',
      value: input.actionPlanSymbol,
      source: 'declared entry leg',
    });
  }
  const conflictingFacts = facts.slice(1).filter((fact) => fact.value !== inherited);
  if (!conflictingFacts.length) return null;

  const evidenceFacts = [facts[0], ...conflictingFacts];
  const compared = conflictingFacts.map((fact) => `${fact.label} ${fact.value}`).join(' and ');
  return {
    summary: `Inherited bot symbol ${inherited} conflicts with ${compared}. Confirm the new run identity before Deploy & start.`,
    signature: evidenceFacts.map((fact) => `${fact.label}:${fact.value}`).join('|'),
    facts: evidenceFacts,
  };
}

export function buildIdentityCoherenceConfirmation(input: {
  confirmed: boolean;
  inheritedSymbol: string;
  signalStream: string;
  actionPlanSymbol: string | null;
}): IdentityCoherenceConfirmation | null {
  if (!input.confirmed) return null;
  const inherited = normalizedSymbol(input.inheritedSymbol);
  if (!inherited) return null;
  return {
    inherited_symbol: inherited,
    signal_stream: input.signalStream || null,
    action_plan_symbol: input.actionPlanSymbol ?? null,
  };
}

export function buildExposureCoherenceEvidence(input: {
  posture: ExposureCoherencePosture | '';
  pendingOrderCount: number | null;
  ownedPositions: Record<string, number>;
  source: string;
  instanceId: string;
  parentRunId: string | null;
}): ExposureCoherenceConflict | null {
  if (!input.posture) return null;
  const blocks = input.posture !== 'FLAT' || input.pendingOrderCount !== 0;
  if (!blocks) return null;
  const pendingLabel = input.pendingOrderCount === null ? 'unknown' : String(input.pendingOrderCount);
  const postureLabel = formatReceiptLabel(input.posture);
  return {
    posture: input.posture,
    pendingOrderCount: input.pendingOrderCount,
    ownedPositions: input.ownedPositions,
    positionsLabel: exposurePositionsLabel(input.ownedPositions),
    source: input.source.trim() || 'request inherited exposure',
    summary: `Inherited exposure is ${postureLabel} with ${pendingLabel} pending order(s). Confirm exposure before Deploy & start.`,
    signature: `${input.instanceId}:${input.parentRunId ?? ''}:${input.posture}:${pendingLabel}:${JSON.stringify(input.ownedPositions)}`,
  };
}

export function buildExposureCoherenceConfirmation(input: {
  evidence: ExposureCoherenceConflict | null;
  confirmed: boolean;
  instanceId: string;
  parentRunId: string | null;
}): ExposureCoherenceConfirmation | null {
  if (input.evidence === null || !input.confirmed) return null;
  return {
    posture: input.evidence.posture,
    pending_order_count: input.evidence.pendingOrderCount,
    owned_positions: input.evidence.ownedPositions,
    strategy_instance_id: input.instanceId || null,
    run_id: input.parentRunId,
  };
}
