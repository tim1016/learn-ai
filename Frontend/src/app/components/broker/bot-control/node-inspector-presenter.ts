import type {
  LiveInstanceStatus,
  OperatorSurfaceEvidenceFact,
  OperatorSurfaceProofLine,
} from '../../../api/live-instances.types';
import { formatReceiptLabel, formatReceiptValue } from '../../../shared/pipes/receipt-label.pipe';
import { fmtTimestampNy } from '../format';

export interface RedeploySettingField {
  readonly id: string;
  readonly label: string;
  readonly value: string;
  readonly detail: string;
}

export type ProofLine = OperatorSurfaceProofLine;

export interface DiagnosticEvidenceLine {
  readonly id: string;
  readonly message: string;
  readonly detail: string | null;
}

export function buildChangeForNextRunFields(status: LiveInstanceStatus): RedeploySettingField[] {
  const startDefaults = status.start_defaults;
  const dailyCap = status.operator_surface.daily_order_cap;
  const sizing = status.sizing;
  const actionPlan = status.operator_surface.action_plan;
  return [
    {
      id: 'daily-order-cap',
      label: 'Daily order cap',
      value: dailyCap.limit === null ? 'Not recorded' : `${dailyCap.limit} orders per day`,
      detail: `${dailyCap.used ?? 'unknown'} used today. Change the cap through redeploy.`,
    },
    {
      id: 'sizing',
      label: 'Sizing preset',
      value: sizing?.preset ?? 'Not recorded',
      detail: `Current sizing source: ${sizingSourceLabel(sizing?.sizing_provenance)}.`,
    },
    {
      id: 'hydrate-policy',
      label: 'Hydrate policy',
      value: hydratePolicyLabel(startDefaults?.hydrate_policy),
      detail: 'Controls how the next run restores prior engine state.',
    },
    {
      id: 'action-plan',
      label: 'Action plan',
      value: actionPlanConsumptionLabel(actionPlan.consumption),
      detail: `Anomaly verdict: ${formatReceiptLabel(actionPlan.anomaly_verdict)}.`,
    },
    {
      id: 'order-mode',
      label: 'Order mode',
      value: orderMode(startDefaults?.readonly),
      detail: 'This is the submit mode that a fresh redeploy will use.',
    },
  ];
}

export function buildProofLines(status: LiveInstanceStatus): ProofLine[] {
  return status.operator_surface.trader_guidance.proof_lines;
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
    case 'account_owner.generation':
      return `AccountOwner generation is ${value}.`;
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

function actionPlanConsumptionLabel(value: string): string {
  switch (value) {
    case 'ACTIVE':
      return 'Active';
    case 'DECLARATIVE_ONLY':
      return 'Declared only';
    case 'UNKNOWN':
      return 'Not recorded';
    default:
      return formatReceiptLabel(value);
  }
}

function orderMode(readonly: boolean | null | undefined): string {
  if (readonly == null) return 'Not recorded';
  return readonly ? 'Read-only observation' : 'Order placement allowed';
}

function hydratePolicyLabel(policy: string | null | undefined): string {
  switch (policy) {
    case 'require':
      return 'Require previous run state';
    case 'optional':
    case 'allow_missing':
      return 'Use previous state when available';
    case 'disabled':
    case 'ignore':
      return 'Start without previous state';
    case null:
    case undefined:
    case '':
      return 'Not recorded';
    default:
      return policy;
  }
}

function sizingSourceLabel(value: string | null | undefined): string {
  switch (value) {
    case 'live_override':
      return 'Live configuration override';
    case 'strategy_default':
      return 'Strategy default';
    case 'pre_policy':
      return 'Pre-policy run';
    case null:
    case undefined:
    case '':
      return 'not recorded';
    default:
      return value;
  }
}
