import type {
  FleetBotHistoryPage,
  FleetBotHistoryRow,
} from '../../components/brokers/alpaca-history/bot-history.service';
import type { GoldenQualificationSummary, StrategyCapability } from '../../components/golden-search/golden-search.types';
import type { StrategyInfo } from '../../components/strategy-lab/strategy-lab.models';
import { TEST_ACCOUNT_ID, TEST_CLERK_ID } from '../../fleet/fleet-directory-testing';
import type { GoldenValidation } from '../../services/golden-validation.types';
import type { StrategyValidationSummary } from '../../services/strategy-validation.types';
import type { StrategyRegistryEntry } from '../signal-strategy-evidence';

/** Data builders for the Signal strategies specs. Plain data only: a spec passes its own `vi`. */

const ALLOWED = { supported: true, reason: null } as const;

export function registryEntry(overrides: Partial<StrategyRegistryEntry> = {}): StrategyRegistryEntry {
  return {
    strategy_key: 'ema_crossover_signal',
    display_name: 'EMA Crossover Signal',
    description: 'Long-only intraday EMA signal generator.\n\nIt reads minute bars and consolidates them.',
    strategy_category: 'production_candidate',
    experimental_notice: null,
    catalog_visible: true,
    program_version: 'ema-crossover-signal/v2',
    rule_statement: 'Long-only EMA(5)/EMA(10) crossover with an RSI(14) filter.',
    validated_settings: { fast_period: 5, slow_period: 10 },
    validated_symbols: ['SPY'],
    parameter_units: { fast_period: 'decision_bars', slow_period: 'decision_bars' },
    capabilities: { sweep: ALLOWED, golden: ALLOWED, live: ALLOWED },
    ...overrides,
  };
}

/** The operational harness: refused a sweep and Golden Search, and capped below Live. */
export function harnessEntry(overrides: Partial<StrategyRegistryEntry> = {}): StrategyRegistryEntry {
  return registryEntry({
    strategy_key: 'deployment_validation',
    display_name: 'Deployment Validation',
    description: 'Minute-bar lifecycle validation strategy.',
    strategy_category: 'operational_validation_harness',
    experimental_notice: 'Experimental validation only — not a trading strategy',
    program_version: 'deployment-validation/v1',
    rule_statement: 'Long-only. Two consecutive green minute bars while flat.',
    validated_settings: {},
    validated_symbols: [],
    parameter_units: {},
    capabilities: {
      sweep: { supported: false, reason: 'This strategy is an operational harness, not a production candidate.' },
      golden: { supported: false, reason: 'This strategy is an operational harness, not a production candidate.' },
      live: { supported: false, reason: 'An operational validation harness runs in Dry Run and Paper only; it stays outside Live.' },
    },
    ...overrides,
  });
}

export function proofRecord(
  strategyKey: string,
  state: 'current' | 'stale' | 'missing' | 'blocked' | 'rejected' | 'unreadable' = 'current',
  overrides: Partial<StrategyValidationSummary> = {},
): StrategyValidationSummary {
  return {
    strategy_key: strategyKey,
    display_name: strategyKey,
    description: '',
    validation_state: 'validated',
    deployable: true,
    validation_case_symbol: 'SPY',
    reconciliation_status: 'reconciled',
    proof: { state, completed_stages: 4, total_stages: 4 },
    ...overrides,
  };
}

export function qualification(
  overrides: Partial<GoldenQualificationSummary> = {},
): GoldenQualificationSummary {
  return {
    id: 'gq-0001',
    program_key: 'ema_crossover_signal',
    program_version: 'ema-crossover-signal/v2',
    symbol: 'SPY',
    parameters: { fast_period: 5, slow_period: 10 },
    status: 'ready',
    status_explanation: 'Ready for the running program.',
    is_default: false,
    created_at_ms: 1_790_000_000_000,
    note: 'approved',
    approved_by: 'local:owner',
    research: {},
    study_id: 'study-1',
    golden_run_id: 7,
    ...overrides,
  };
}

export function configurationReview(overrides: Partial<GoldenValidation> = {}): GoldenValidation {
  return {
    id: 1,
    source_run_id: 41,
    label: null,
    strategy_name: 'ema_crossover_signal',
    symbol: 'SPY',
    rationale: 'because',
    designated_by: 'local:owner',
    designated_at_ms: 1_790_000_000_000,
    state: 'accepted',
    validation_case: {
      schema_version: 1,
      source_run_id: 41,
      strategy: { name: 'ema_crossover_signal', program_version: 'ema-crossover-signal/v2' },
      symbol: 'SPY',
      parameters: { fast_period: 6, slow_period: 12 },
      window: { start_ms: 1, end_ms: 2, timespan: 'minute' },
      data_policy: null,
      execution: { fill_mode: 'next_open', initial_cash: 100000, commission_per_order: null, brokerage_policy: null, configuration: null },
      requested_engine: null,
      parity_group_id: null,
    },
    evidence_state: 'current',
    evidence_revision: 'r'.repeat(64),
    evidence_applicability: { status: 'current', explanation: 'Evidence applies.', requires_manual_override: false },
    parity_evidence: {},
    latest_review: {
      id: 1,
      decision: 'accept',
      classification: 'engine_agreement',
      evidence_state: 'current',
      reason: 'Engines agree.',
      quantconnect_backtest_id: null,
      authorized_program_version: null,
      reviewed_by: 'local:owner',
      reviewed_at_ms: 1_790_000_100_000,
      expected_evidence_revision: 'r'.repeat(64),
    },
    review_is_current: true,
    reviews: [],
    ...overrides,
  };
}

export function runningBot(overrides: Partial<FleetBotHistoryRow> = {}): FleetBotHistoryRow {
  return {
    broker: 'alpaca',
    clerk_id: TEST_CLERK_ID,
    strategy_instance_id: 'spy-ema-20261020-0931',
    strategy_key: 'ema_crossover_signal',
    strategy_label: 'EMA Crossover Signal',
    symbol: 'SPY',
    account_id: TEST_ACCOUNT_ID.toUpperCase(),
    world: 'paper',
    world_label: 'PAPER · practice money',
    status: 'running',
    status_label: 'Running',
    started_at_ms: 1_790_000_000_000,
    stopped_at_ms: null,
    outcome: null,
    transaction_count: 0,
    orders: { sent: 0, filled: 0, cancelled: 0, rejected: 0 },
    budget_usd: '1000.00',
    result_usd: null,
    fees_usd: null,
    money_unavailable_reason: null,
    money_scope_note: null,
    runs: [],
    page_unavailable_reason: null,
    ...overrides,
  };
}

export function fleetPage(overrides: Partial<FleetBotHistoryPage> = {}): FleetBotHistoryPage {
  const rows = overrides.rows ?? [runningBot()];
  return {
    observed_at_ms: 1_790_001_000_000,
    rows,
    gaps: [],
    total: rows.length,
    page: 1,
    page_size: 25,
    symbols: ['SPY'],
    ...overrides,
  };
}

export function strategyInfo(overrides: Partial<StrategyInfo> = {}): StrategyInfo {
  return {
    name: 'ema_crossover_signal',
    display_name: 'EMA Crossover Signal',
    description: '',
    params_schema: {
      title: 'EmaCrossoverSignalParams',
      type: 'object',
      properties: {
        fast_period: { type: 'integer', default: 5, minimum: 2, maximum: 200, title: 'Fast Period' },
        slow_period: { type: 'integer', default: 10, minimum: 3, maximum: 400, title: 'Slow Period' },
        gap: { type: 'number', default: 0.2, minimum: 0, title: 'Gap' },
      },
    },
    supported_resolutions: ['minute'],
    strategy_bars: { timespan: 'minute', multiplier: 15 },
    ...overrides,
  };
}

export function goldenDeclaration(overrides: Partial<StrategyCapability> = {}): StrategyCapability {
  return {
    strategy_key: 'ema_crossover_signal',
    display_name: 'EMA Crossover Signal',
    available: true,
    reason: null,
    knobs: [
      {
        name: 'fast_period',
        label: 'Fast EMA length',
        unit: 'decision bars',
        kind: 'integer',
        domain_low: 2,
        domain_high: 30,
        quantum: 1,
        default_step: 1,
        warmup_dependent: true,
        default_value: 5,
        note: '',
      },
    ],
    fixed: [],
    constraints: [{ left: 'fast_period', op: '<', right: 'slow_period', message: 'The fast EMA length must be shorter than the slow one.' }],
    default_pair_audits: [],
    default_expected_trades_per_year: 20,
    importance: { low: 0.1, high: 0.9, default: 0.5 },
    ...overrides,
  };
}
