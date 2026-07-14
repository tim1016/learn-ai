import type {
  AccountConditionRow,
  AccountFreezeBanner,
  AccountReconciliationAutomationPolicy,
  AccountReconciliationReceipt,
  AccountObservationView,
  AccountTriageBotRef,
  AccountTriageResponse,
} from '../../../api/account-reconciliation.types';

interface AccountTriageFixtureOptions {
  accountId?: string;
  generatedAtMs?: number;
  receipt?: AccountReconciliationReceipt | null;
  reconciliationValidUntilMs?: number | null;
  automationPolicy?: AccountReconciliationAutomationPolicy;
  accountObservation?: AccountObservationView;
  summaryHeadline?: string;
  summaryDetail?: string;
  gate?: Partial<AccountTriageResponse['overall_gate_result']>;
  conditions?: AccountConditionRow[];
  freezeBanner?: AccountFreezeBanner | null;
  clearFreezeActionable?: boolean;
  affectedBots?: AccountTriageBotRef[];
}

interface AccountFreezeConditionOptions {
  accountId?: string;
  generatedAtMs?: number;
  conditionType?: AccountConditionRow['condition_type'];
  owner?: Partial<AccountConditionRow['owner']>;
  severity?: AccountConditionRow['severity'];
  title?: string;
  detail?: string;
  operatorNextStep?: string | null;
  source?: string;
  affectedStrategyInstanceIds?: string[];
  cureAction?: AccountConditionRow['cure_action'];
}

interface FrozenAccountTriageFixtureOptions extends AccountTriageFixtureOptions {
  condition?: AccountConditionRow;
  conditionOptions?: AccountFreezeConditionOptions;
}

export function makeCleanAccountTriage(
  options: AccountTriageFixtureOptions = {},
): AccountTriageResponse {
  const accountId = options.accountId ?? 'DU1234567';
  const generatedAtMs = options.generatedAtMs ?? 1_780_000_002_000;
  const receipt = options.receipt ?? null;
  return {
    schema_version: 1,
    generated_at_ms: generatedAtMs,
    account_id: accountId,
    strategy_instance_id: null,
    summary_headline: options.summaryHeadline ?? 'Account recovery gates passing',
    summary_detail:
      options.summaryDetail ?? `Account ${accountId} has no blocking account triage rows.`,
    overall_gate_result: {
      gate_id: 'account.triage',
      status: 'pass',
      source: 'account_triage',
      operator_reason: `Account ${accountId} has no blocking account triage rows.`,
      operator_next_step: 'ACCOUNT_TRIAGE_PASSING',
      evidence_at_ms: generatedAtMs,
      ...options.gate,
    },
    account_reconciliation_receipt: receipt,
    account_reconciliation_valid_until_ms:
      options.reconciliationValidUntilMs ?? receipt?.expires_at_ms ?? null,
    reconciliation_automation_policy: options.automationPolicy ?? {
      schema_version: 1,
      account_id: accountId,
      enabled: false,
      updated_at_ms: 0,
      updated_by: 'system.default',
    },
    account_observation: options.accountObservation ?? {
      state: 'ABSENT',
      reason_line: 'Account verification is not available yet.',
      observed_at_ms: null,
      valid_until_ms: null,
      history: [],
    },
    gate_rows: [],
    conditions: options.conditions ?? [],
    freeze_banner: options.freezeBanner ?? null,
    clear_freeze_actionable: options.clearFreezeActionable ?? false,
    affected_bots: options.affectedBots ?? [],
  };
}

export function makeAccountFreezeCondition(
  options: AccountFreezeConditionOptions = {},
): AccountConditionRow {
  const accountId = options.accountId ?? 'DU1234567';
  const generatedAtMs = options.generatedAtMs ?? 1_780_000_002_500;
  return {
    condition_type: options.conditionType ?? 'account_freeze',
    scope: 'account',
    owner: {
      owner_type: 'account',
      owner_id: accountId,
      label: `Account ${accountId}`,
      strategy_instance_id: null,
      run_id: null,
      lifecycle_state: null,
      ...options.owner,
    },
    severity: options.severity ?? 'critical',
    title: options.title ?? 'Account freeze active',
    detail: options.detail ?? 'manual_freeze',
    operator_next_step: options.operatorNextStep ?? 'CLEAR_FREEZE',
    source: options.source ?? 'manual_freeze',
    evidence_at_ms: generatedAtMs,
    evidence_refs: [],
    affected_strategy_instance_ids: options.affectedStrategyInstanceIds ?? ['DEPVALSPYJUL8'],
    cure_action: options.cureAction ?? 'clear_freeze',
  };
}

export function makeFrozenAccountTriage(
  options: FrozenAccountTriageFixtureOptions = {},
): AccountTriageResponse {
  const accountId = options.accountId ?? 'DU1234567';
  const generatedAtMs = options.generatedAtMs ?? 1_780_000_002_500;
  const condition =
    options.condition ??
    makeAccountFreezeCondition({
      accountId,
      generatedAtMs,
      ...options.conditionOptions,
    });
  return makeCleanAccountTriage({
    accountId,
    generatedAtMs,
    receipt: options.receipt,
    summaryHeadline: options.summaryHeadline ?? 'Account recovery needs attention',
    summaryDetail: options.summaryDetail ?? condition.detail,
    gate: {
      gate_id: 'account.triage',
      status: 'freeze',
      source: condition.source,
      operator_reason: condition.detail,
      operator_next_step: condition.operator_next_step ?? 'CHECK_IBKR',
      evidence_at_ms: condition.evidence_at_ms,
      ...options.gate,
    },
    conditions: options.conditions ?? [condition],
    freezeBanner: options.freezeBanner ?? {
      headline: 'Account sick bay is gating new starts.',
      detail: 'Run account reconciliation and clear the active account freeze before deploying.',
    },
    clearFreezeActionable: options.clearFreezeActionable ?? condition.cure_action === 'clear_freeze',
    affectedBots: options.affectedBots ?? [],
  });
}
