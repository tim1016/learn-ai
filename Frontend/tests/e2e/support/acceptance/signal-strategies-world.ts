import { EMA_STRATEGY } from '../../../../src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.fixtures';
import type { DeployBotView } from '../../../../src/app/components/broker/v2-panel/lib/broker-v2-panel.service';
import type { FleetBotHistoryPage } from '../../../../src/app/components/brokers/alpaca-history/bot-history.service';
import type {
  GoldenQualificationSummary,
  QualificationDeployOffer,
  StrategyCapability,
  StudySummary,
} from '../../../../src/app/components/golden-search/golden-search.types';
import { deployOffer, emaCapability } from '../../../../src/app/components/golden-search/testing/fixtures';
import type { StrategyInfo } from '../../../../src/app/components/strategy-lab/strategy-lab.models';
import type { GoldenValidation } from '../../../../src/app/services/golden-validation.types';
import type { StrategyValidationSummary } from '../../../../src/app/services/strategy-validation.types';
import { DEPLOY_LANES, LIVE_API, NOW_MS, PAPER_API } from '../owner-walk-world';
import type { Handler } from './mock-backend';
import { ok } from './mock-backend';

/**
 * The Signal strategies side of walk (b): one registered strategy, EMA
 * Crossover Signal, with a proof on file, an approved study and two qualified
 * versions for SPY (the current default and an earlier one) and one stale
 * version for QQQ, answered at the network edge.
 *
 * Every payload is typed against a contract this branch already has. The
 * registry's own entry is the exception: its schema (`StrategyRegistryEntry`
 * in the generated `broker.types.ts`) arrives with the Signal strategies place
 * (bundle 5, PR #3542), so {@link RegistryEntry} mirrors it by hand, field for
 * field, until that merges.
 */

export const EMA_KEY = 'ema_crossover_signal';
export const EMA_LABEL = 'EMA Crossover Signal';
export const EMA_PROGRAM = 'ema-crossover-signal/v3';

/** The default SPY version, the one Deploy is carried to. */
export const DEFAULT_VERSION_ID = 'gq-9c1e5a07b2d84f3aa6e0c4d17b58f2e1';
/** An earlier, still ready SPY version with its own exact settings. */
export const EARLIER_VERSION_ID = 'gq-3b7d90e4a1c2486f8d5e7a09c6b14d23';
/** A QQQ version whose program changed since it was approved. */
export const STALE_VERSION_ID = 'gq-61f4a8c3d0e94b7aa2c5e8f1b3d70964';
export const STUDY_ID = 'study-spy-ema-0001-aaaa';

/** The exact settings of each version, as the list of versions and Deploy's offer both carry them. */
type Settings = QualificationDeployOffer['parameters'];
export const DEFAULT_SETTINGS: Settings = { gap: 0.15, gap_bps: 0, rsi_min: 48, rsi_max: 72, fast_period: 8, slow_period: 21, hold_bars: 4 };
export const EARLIER_SETTINGS: Settings = { gap: 0.25, gap_bps: 0, rsi_min: 46, rsi_max: 74, fast_period: 6, slow_period: 12, hold_bars: 6 };

interface CapabilityAnswer {
  readonly supported: boolean;
  readonly reason: string | null;
}

/** Mirrors `StrategyRegistryEntry` in bundle 5's `Frontend/src/app/api/broker.types.ts`. */
export interface RegistryEntry {
  readonly strategy_key: string;
  readonly display_name: string;
  readonly description: string;
  readonly strategy_category: 'production_candidate' | 'operational_validation_harness';
  readonly experimental_notice: string | null;
  readonly catalog_visible: boolean;
  readonly program_version: string | null;
  readonly rule_statement: string | null;
  readonly validated_settings: Readonly<Record<string, string | number | boolean>>;
  readonly validated_symbols: readonly string[];
  readonly parameter_units: Readonly<Record<string, string>>;
  readonly capabilities: {
    readonly sweep: CapabilityAnswer;
    readonly golden: CapabilityAnswer;
    readonly live: CapabilityAnswer;
  };
}

const ALLOWED: CapabilityAnswer = { supported: true, reason: null };
const HARNESS_REASON = 'This strategy is an operational harness, not a production candidate.';

export const REGISTRY: readonly RegistryEntry[] = [
  {
    strategy_key: EMA_KEY,
    display_name: EMA_LABEL,
    description: 'Long-only intraday EMA signal generator.\n\nIt reads minute bars and consolidates them.',
    strategy_category: 'production_candidate',
    experimental_notice: null,
    catalog_visible: true,
    program_version: EMA_PROGRAM,
    rule_statement: 'Long-only EMA(5)/EMA(10) crossover with an RSI(14) filter.',
    validated_settings: { fast_period: 5, slow_period: 10 },
    validated_symbols: ['SPY'],
    parameter_units: { fast_period: 'decision_bars', slow_period: 'decision_bars' },
    capabilities: { sweep: ALLOWED, golden: ALLOWED, live: ALLOWED },
  },
  {
    strategy_key: 'deployment_validation',
    display_name: 'Deployment Validation',
    description: 'Minute-bar lifecycle validation strategy.',
    strategy_category: 'operational_validation_harness',
    experimental_notice: 'Experimental validation only — not a trading strategy',
    catalog_visible: true,
    program_version: 'deployment-validation/v1',
    rule_statement: 'Long-only. Two consecutive green minute bars while flat.',
    validated_settings: {},
    validated_symbols: [],
    parameter_units: {},
    capabilities: {
      sweep: { supported: false, reason: HARNESS_REASON },
      golden: { supported: false, reason: HARNESS_REASON },
      live: { supported: false, reason: 'An operational validation harness runs in Dry Run and Paper only; it stays outside Live.' },
    },
  },
];

const proof = (strategyKey: string, displayName: string): StrategyValidationSummary => ({
  strategy_key: strategyKey,
  display_name: displayName,
  description: '',
  validation_state: 'validated',
  deployable: true,
  validation_case_symbol: 'SPY',
  reconciliation_status: 'reconciled',
  proof: { state: 'current', completed_stages: 4, total_stages: 4 },
});

const version = (
  id: string,
  symbol: string,
  parameters: Settings,
  overrides: Partial<GoldenQualificationSummary>,
): GoldenQualificationSummary => ({
  id,
  program_key: EMA_KEY,
  program_version: EMA_PROGRAM,
  symbol,
  parameters,
  status: 'ready',
  status_explanation: 'Ready for the running program.',
  is_default: false,
  created_at_ms: NOW_MS - 20 * 86_400_000,
  note: 'Accept this exact configuration after reviewing the stated limitations.',
  approved_by: 'local:owner',
  research: { claim: 'confirmatory', exam_outcome: 'meets_rules', exposure_state: 'opened_once' },
  study_id: STUDY_ID,
  golden_run_id: 7,
  ...overrides,
});

export const VERSIONS: readonly GoldenQualificationSummary[] = [
  version(DEFAULT_VERSION_ID, 'SPY', DEFAULT_SETTINGS, { is_default: true, created_at_ms: NOW_MS - 11 * 86_400_000 }),
  version(EARLIER_VERSION_ID, 'SPY', EARLIER_SETTINGS, { created_at_ms: NOW_MS - 33 * 86_400_000, golden_run_id: 5, study_id: 'study-spy-ema-0000-bbbb' }),
  version(STALE_VERSION_ID, 'QQQ', DEFAULT_SETTINGS, {
    status: 'stale',
    status_explanation: 'The program changed after this version was approved; re-prove it before it can be deployed.',
    golden_run_id: 3,
    study_id: 'study-qqq-ema-0001-cccc',
  }),
];

const SETTINGS_BY_VERSION: ReadonlyMap<string, Settings> = new Map([
  [DEFAULT_VERSION_ID, DEFAULT_SETTINGS],
  [EARLIER_VERSION_ID, EARLIER_SETTINGS],
  [STALE_VERSION_ID, DEFAULT_SETTINGS],
]);

/** What Deploy reads for one version, `GET /api/research/golden-qualifications/:id/deploy-offer`. */
export function offerFor(found: GoldenQualificationSummary): QualificationDeployOffer {
  return deployOffer({
    qualification_id: found.id,
    program_key: found.program_key,
    program_version: found.program_version,
    symbol: found.symbol,
    parameters: SETTINGS_BY_VERSION.get(found.id) ?? {},
    is_default: found.is_default,
    status: found.status,
    explanation: found.status_explanation,
  });
}

const APPROVED_STUDY: StudySummary = {
  id: STUDY_ID,
  parent_study_id: null,
  strategy_key: EMA_KEY,
  symbol: 'SPY',
  state: 'approved',
  presented_status: 'completed',
  revision: 9,
  created_at_ms: NOW_MS - 12 * 86_400_000,
  protocol_hash: 'abcdef0123456789',
  method: 'zoom',
  consumed_evaluations: 1590,
  budget_cap: 5000,
  cache_hits: 37,
  invalid_points: 6,
  incomplete: false,
  failure_reason: null,
  hidden: false,
  exposure_claim: 'confirmatory',
  exam_outcome: 'meets_rules',
  qualification_id: DEFAULT_VERSION_ID,
};

/** Strategy Lab's declared schema for the strategy, so Define and Explore can name its settings. */
const EMA_SCHEMA: StrategyInfo = {
  name: EMA_KEY,
  display_name: EMA_LABEL,
  description: '',
  params_schema: {
    title: 'EmaCrossoverSignalParams',
    type: 'object',
    properties: {
      gap: { type: 'number', default: 0.2, minimum: 0, title: 'Gap' },
      rsi_min: { type: 'number', default: 50, minimum: 0, maximum: 100, title: 'RSI lower gate' },
      rsi_max: { type: 'number', default: 70, minimum: 0, maximum: 100, title: 'RSI upper gate' },
      fast_period: { type: 'integer', default: 5, minimum: 2, maximum: 200, title: 'Fast Period' },
      slow_period: { type: 'integer', default: 10, minimum: 3, maximum: 400, title: 'Slow Period' },
      hold_bars: { type: 'integer', default: 5, minimum: 1, maximum: 26, title: 'Hold bars' },
    },
  },
  supported_resolutions: ['minute'],
  strategy_bars: { timespan: 'minute', multiplier: 15 },
};

const GOLDEN_DECLARATIONS: readonly StrategyCapability[] = [emaCapability()];
const NO_REVIEWS: readonly GoldenValidation[] = [];

/** No bot of the strategy is running anywhere: the fleet's answer is complete and empty. */
const NO_RUNNING_BOTS: FleetBotHistoryPage = {
  observed_at_ms: NOW_MS,
  rows: [],
  gaps: [],
  total: 0,
  page: 1,
  page_size: 25,
  symbols: [],
};

const OFFER_PATH = /^\/api\/research\/golden-qualifications\/([^/]+)\/deploy-offer$/;

/**
 * What the strategy's pages read: the registry, the proof catalog, the
 * qualified versions and the offer of each, Golden Search's studies and
 * declarations, the baseline-run reviews, the fleet's running bots and
 * Strategy Lab's parameter schemas. All of it is reads.
 */
export const signalStrategyHandlers: Handler = ({ method, path }) => {
  if (method !== 'GET') return null;
  if (path === '/api/engine/strategy-registry') return ok(REGISTRY);
  if (path === '/api/engine/strategies') return ok([EMA_SCHEMA]);
  if (path === '/api/strategy-validation/strategies') {
    return ok({ strategies: REGISTRY.map((entry) => proof(entry.strategy_key, entry.display_name)) });
  }
  if (path === '/api/research/golden-qualifications') return ok(VERSIONS);
  const offered = OFFER_PATH.exec(path);
  if (offered !== null) {
    const found = VERSIONS.find((candidate) => candidate.id === decodeURIComponent(offered[1]));
    return found === undefined ? { json: { detail: 'No such golden configuration.' }, status: 404 } : ok(offerFor(found));
  }
  if (path === '/api/research/golden-search/capabilities') return ok(GOLDEN_DECLARATIONS);
  if (path === '/api/research/golden-search/studies') return ok([APPROVED_STUDY]);
  if (path === '/api/research/golden-validations') return ok(NO_REVIEWS);
  if (path === '/api/research/backtest-runs') return ok([]);
  if (path === '/api/broker-clerks/aggregate/bot-history') return ok(NO_RUNNING_BOTS);
  return null;
};

/** The strategy as Deploy's own list declares it: every setting a version carries has a field. */
const EMA_FOR_DEPLOY: DeployBotView['strategies'][number] = {
  ...EMA_STRATEGY,
  params_schema: {
    properties: {
      gap: { type: 'number', default: 0.2, title: 'Crossover gap' },
      gap_bps: { type: 'number', default: 0, title: 'Crossover gap (bps)' },
      rsi_min: { type: 'number', default: 50, title: 'RSI lower gate' },
      rsi_max: { type: 'number', default: 70, title: 'RSI upper gate' },
      fast_period: { type: 'integer', default: 5, title: 'Fast EMA length' },
      slow_period: { type: 'integer', default: 10, title: 'Slow EMA length' },
      hold_bars: { type: 'integer', default: 5, title: 'Hold time' },
    },
  },
};

const withFullEma = (view: DeployBotView): DeployBotView => ({
  ...view,
  strategies: view.strategies.map((strategy) => (strategy.strategy_key === EMA_KEY ? EMA_FOR_DEPLOY : strategy)),
});

/** An account's Deploy view, with the strategy's whole schema in place of the owner walk's one-field version. Ask it before `deployReadHandlers`. */
export const emaDeployViewHandlers: Handler = ({ method, path }) => {
  if (method !== 'GET') return null;
  if (path === `${PAPER_API}/bots/deploy`) return ok(withFullEma(DEPLOY_LANES.paper.view));
  if (path === `${LIVE_API}/bots/deploy`) return ok(withFullEma(DEPLOY_LANES.live.view));
  return null;
};
