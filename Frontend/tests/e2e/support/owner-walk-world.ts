import type { Page, Request, Route } from '@playwright/test';

import type { components } from '../../../src/app/api/broker.types';
import type {
  AlpacaLiveVerdict,
  BrokerAccountSnapshot,
  ClerkStatus,
  SqliteRecoveryActionCheck,
} from '../../../src/app/api/alpaca.types';
import {
  DEPLOY_VIEW,
  LIVE_DEPLOY_VIEW,
} from '../../../src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.fixtures';
import type {
  AccountMoneyView,
  BudgetDeployReceipt,
  DeployBotBody,
  DeployBotView,
  DeploymentBudgetPreview,
  DeploymentBudgetView,
  DeploySubmissionBody,
  MoneySegment,
  RunAdmissionDecision,
} from '../../../src/app/components/broker/v2-panel/lib/broker-v2-panel.service';
import type {
  BotCatalogView,
  BotClearRequest,
  BotPanelLiveSnapshot,
  BotPanelView,
  CohortActionResult,
  CohortLegResult,
  PanelAction,
  PanelActionRequest,
  PanelActionResult,
  PanelProfile,
} from '../../../src/app/components/broker/v2-panel/lib/broker-v2-panel.types';
import type { FleetDirectoryResponse, LaneDescriptor } from '../../../src/app/fleet/fleet-directory.types';
import type { CommandContext } from '../../../src/app/fleet/resource-target';
import type { AggregateAttentionResponse, LaneAttentionItem } from '../../../src/app/services/lane-attention.service';
import { fakeAccountMoney } from '../../../src/app/testing/account-money-fixtures';
import { fakeAlpacaLiveVerdict } from '../../../src/app/testing/alpaca-live-verdict-fixtures';
import {
  fakeBotPanelView,
  fakeCatalogBot,
  fakeChartFeed,
  fakePanelAction,
  fakeSqliteStopAction,
} from '../../../src/app/testing/bot-panel-fixtures';

/**
 * The backend an owner walks through in `account-owner-walk-through.spec.ts`
 * (PRD #2560 "Daily browser walk-through"): two ready Alpaca lanes, Paper and
 * Live, answered entirely at the network edge. Nothing here reaches a clerk,
 * a coordinator or a broker, so no order can ever be placed.
 *
 * Every response is built from the real contract types (`satisfies`, or the
 * canonical unit factories in `src/app/testing/`), so a contract change
 * breaks `tsc -p tests/e2e/tsconfig.json` rather than this walk drifting.
 * Every dollar is a Python-shaped authored string and the walk asserts the
 * page shows it verbatim; the browser adds nothing up.
 *
 * The world is a small state machine keyed on the commands the page has
 * sent: a Deploy makes the bot run (holding 1 SPY), a Stop leaves it stopped
 * but still holding, the flatten sequence's sale makes it flat and Finished,
 * and a Clear takes it off Home. Each read answers the current state, so the
 * page's own polls carry every change onto the screen.
 */

type Schemas = components['schemas'];
type SymbolCatalogEntry = Schemas['SymbolCatalogEntry'];
type SymbolCoverageSpan = Schemas['SymbolCoverageSpan'];
type RecoveryActionCheckRequest = Schemas['RecoveryActionCheckRequest'];

/** Every request body a command carries: the payload plus the fleet envelope. */
type Enveloped<T> = T & { readonly command_context: CommandContext };

export const NOW_MS = 1_790_000_000_000;

export const PAPER_CLERK = 'clrk-paper-walk';
export const PAPER_ACCOUNT = 'PA3WALK0001';
export const LIVE_CLERK = 'clrk-live-walk';
export const LIVE_ACCOUNT = '9LIVE0001';

export const PAPER_WORKSPACE = `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}`;
export const LIVE_WORKSPACE = `/brokers/alpaca/clerks/${LIVE_CLERK}/accounts/${LIVE_ACCOUNT}`;

/** The name the backend authors at the Deploy (#2551); the page never makes one up. */
export const WALKED_BOT = 'spy-dv-20260928-1031';
/** A bot that finished days ago and stays on Home: its clear is refused. */
export const EARLIER_BOT = 'qqq-dv-20260925-1402';
/** The Live bot the typed-consent Deploy names. */
export const LIVE_BOT = 'spy-dv-20260928-1107';

const BINDING_GENERATION = 3;
const ROUTING_EPOCH = 4;

const CLERK_SCOPE = (clerk: string): string => `/api/brokers/alpaca/clerks/${clerk}`;
const ACCOUNT_SCOPE = (clerk: string, account: string): string => `${CLERK_SCOPE(clerk)}/accounts/${account}`;
/** Each account's API root: every read and command of its workspace lives under it. */
export const PAPER_API = ACCOUNT_SCOPE(PAPER_CLERK, PAPER_ACCOUNT);
export const LIVE_API = ACCOUNT_SCOPE(LIVE_CLERK, LIVE_ACCOUNT);
const PAPER = PAPER_API;
const LIVE = LIVE_API;

// ── Python-authored copy, verbatim from the backend's own tables ───────────

/** `deploy_submissions.bot_name_note`. */
export const BOT_NAME_NOTE =
  'Named at Deploy: spy-dv-YYYYMMDD-HHMM, from the New York minute the Deploy is committed.';
/** `budget_deploy._RECEIPT_COPY["deployed"]` and its next action. */
export const RECEIPT_MESSAGE = `${WALKED_BOT} is deployed`;
export const RECEIPT_EXPLANATION = '$1000.00 is set aside for it.';
const RECEIPT_NEXT_ACTION = 'Open the bot\'s page to watch it trade.';
/** `sqlite_panel_adapter` row explanations (PRD #2560 D7). */
export const RUNNING_EXPLANATION = 'Running · holds 1 SPY';
export const HOLDING_EXPLANATION = 'Stopped · still holds 1 SPY · no bot is managing it';
const FINISHED_EXPLANATION = 'Off duty and flat.';
/** `sqlite_panel_source._outcome_message` for the Clerk's stop. */
export const STOP_MESSAGE = 'Stop bot decisions completed.';
/** `lane_summary._stopped_holding_item`. */
export const HOLDING_ATTENTION = `${WALKED_BOT} is stopped but still holds 1 SPY. No bot is managing it.`;
/** `sqlite_panel_source._outcome_message` for each flatten command. */
export const RECONCILE_MESSAGE = 'Reconcile now completed.';
export const PLAN_NEXT_STEP = 'Send the prepared sale of 1 SPY at market.';
export const SELL_MESSAGE = 'Sell the prepared position completed.';
/** `panel_data_source._archive`, and `action_policy._ARCHIVE_BLOCKER_COPY`. */
const ARCHIVE_MESSAGE = 'Bot archived and taken off the roster. Its history and receipts are kept; it can start no new runs.';
export const CLEAR_REFUSAL = {
  reason_code: 'ARCHIVE_CUSTODY_UNPROVABLE',
  message: 'This account cannot prove the bot is flat.',
  why: 'A bot is cleared only on proof that it holds nothing. Choose Reconcile now once Alpaca can be read, then clear it.',
} as const;

/** The amounts this walk types, and the Live phrase the backend binds consent to. */
export const PAPER_BUDGET = '1000.00';
export const LIVE_BUDGET = '800.00';
export const LIVE_PHRASE = `DEPLOY ${LIVE_ACCOUNT} $${LIVE_BUDGET}`;

// ── The fleet ──────────────────────────────────────────────────────────────

function lane(clerkId: string, label: string, account: string, mode: 'paper' | 'live'): LaneDescriptor {
  return {
    broker: 'alpaca',
    clerk_id: clerkId,
    display_label: label,
    lifecycle_state: 'ready',
    volume_id: `vol-${clerkId}`,
    last_seen_at_ms: NOW_MS,
    routing_epoch: ROUTING_EPOCH,
    effective_binding_generation: BINDING_GENERATION,
    capabilities: [
      'account_read', 'positions_read', 'orders_read', 'bot_panel_read', 'bot_action',
      'configuration_manage', 'custody_read', 'deploy', 'gallery_read',
    ],
    provider_summary: {
      provider_id: 'alpaca',
      adapter_version: 'alpaca-fleet.4',
      confirmed_account_id: account,
      confirmed_binding_generation: BINDING_GENERATION,
      endpoint_mode: mode,
      authority_state: mode === 'paper' ? 'real_paper' : 'real_live',
      running_count: 0,
      dry_run_count: 0,
      attention_count: 0,
    },
    observed_at_ms: NOW_MS,
  };
}

const DIRECTORY = {
  observed_at_ms: NOW_MS,
  clerks: [
    lane(PAPER_CLERK, 'Paper', PAPER_ACCOUNT, 'paper'),
    lane(LIVE_CLERK, 'Live', LIVE_ACCOUNT, 'live'),
  ],
} satisfies FleetDirectoryResponse;

function brokerAccount(accountId: string, mode: 'paper' | 'live', equity: number): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: accountId,
    account_mode: mode,
    account_status: 'ACTIVE',
    account_blocked: false,
    trading_blocked: false,
    pattern_day_trader: false,
    currency: 'USD',
    cash: equity,
    equity,
    buying_power: equity,
    portfolio_value: equity,
    long_market_value: 0,
    short_market_value: 0,
    created_at_ms: null,
    observed_at_ms: NOW_MS,
  };
}

function clerkStatus(accountId: string, world: 'real_paper' | 'real_live'): ClerkStatus {
  return {
    account_id: accountId,
    authority_kind: world,
    broker: 'alpaca',
    hold: { active: false },
    observed_at_ms: NOW_MS,
    outstanding_intents: 0,
  };
}

function verdict(finalVerdict: AlpacaLiveVerdict['final_verdict'], accountId: string): AlpacaLiveVerdict {
  return fakeAlpacaLiveVerdict(finalVerdict, { observed_account_id: accountId, observed_at_ms: NOW_MS });
}

// ── Money: one account-money read per state (PRD #2560 D12) ────────────────

const free = (amount: string, bps: number): MoneySegment =>
  ({ kind: 'free', label: 'free to deploy', amount_usd: amount, share_bps: bps });

/** One ready read of an account holding nothing: all of it free to deploy. */
function idleMoney(accountId: string, world: 'real_paper' | 'real_live', total: string): AccountMoneyView {
  return fakeAccountMoney({
    world,
    account_id: accountId,
    observed_at_ms: NOW_MS,
    total_usd: total,
    cash_usd: total,
    free_to_deploy_usd: total,
    in_bots_usd: '0.00',
    held_by_stopped_usd: '0.00',
    outside_bots_usd: '0.00',
    account_charges_usd: '0.00',
    settling_usd: '0.00',
    stopped_holding_count: 0,
    open_pnl_usd: null,
    equity_usd: total,
    today_pnl_usd: '0.00',
    segments: [free(total, 10_000)],
  });
}

/** Before the Deploy: $25,000.00, all free. */
export const PAPER_MONEY_BEFORE = idleMoney(PAPER_ACCOUNT, 'real_paper', '25000.00');
/** Deploy's Money step: the same account with the proposed $1,000.00 carved from free. */
const PAPER_MONEY_WITH_NEW = fakeAccountMoney({
  ...PAPER_MONEY_BEFORE,
  free_to_deploy_usd: '24000.00',
  segments: [
    { kind: 'new', label: 'new bot', amount_usd: PAPER_BUDGET, share_bps: 400 },
    free('24000.00', 9_600),
  ],
});
/** Running: its $1,000.00 slice, 1 SPY bought at $500.00 and $500.00 still free in it. */
export const PAPER_MONEY_RUNNING = fakeAccountMoney({
  ...PAPER_MONEY_BEFORE,
  cash_usd: '24500.00',
  free_to_deploy_usd: '24000.00',
  in_bots_usd: PAPER_BUDGET,
  open_pnl_usd: '0.00',
  segments: [
    {
      kind: 'bot',
      strategy_instance_id: WALKED_BOT,
      label: WALKED_BOT,
      amount_usd: PAPER_BUDGET,
      share_bps: 400,
      parts: {
        in_shares_usd: '500.00', in_shares_bps: 5_000,
        pending_usd: '0.00', pending_bps: 0,
        free_usd: '500.00', free_bps: 5_000,
      },
      palette_index: 0,
    },
    free('24000.00', 9_600),
  ],
});
/** Stopped, still holding: the shares' cost stays; the free $500.00 is released. */
export const PAPER_MONEY_HOLDING = fakeAccountMoney({
  ...PAPER_MONEY_RUNNING,
  free_to_deploy_usd: '24500.00',
  in_bots_usd: '0.00',
  held_by_stopped_usd: '500.00',
  stopped_holding_count: 1,
  segments: [
    {
      kind: 'stopped',
      strategy_instance_id: WALKED_BOT,
      label: `held by stopped bot ${WALKED_BOT}`,
      amount_usd: '500.00',
      share_bps: 200,
      released_usd: '500.00',
      still_claimed_usd: '0.00',
      palette_index: 0,
    },
    free('24500.00', 9_800),
  ],
});
/** Flat: the sale at $500.99 returned everything, and the bot finished $0.99 up. */
export const PAPER_MONEY_FINISHED = idleMoney(PAPER_ACCOUNT, 'real_paper', '25000.99');

const LIVE_MONEY = idleMoney(LIVE_ACCOUNT, 'real_live', '5000.00');
const LIVE_MONEY_WITH_NEW = fakeAccountMoney({
  ...LIVE_MONEY,
  free_to_deploy_usd: '4200.00',
  segments: [
    { kind: 'new', label: 'new bot', amount_usd: LIVE_BUDGET, share_bps: 1_600 },
    free('4200.00', 8_400),
  ],
});

// ── Deploy ─────────────────────────────────────────────────────────────────

const PAPER_DEPLOY_VIEW = {
  ...DEPLOY_VIEW,
  account_id: PAPER_ACCOUNT,
  account_label: `Alpaca paper · ${PAPER_ACCOUNT}`,
  evaluated_at_ms: NOW_MS,
} satisfies DeployBotView;
const LIVE_ACCOUNT_DEPLOY_VIEW = {
  ...LIVE_DEPLOY_VIEW,
  account_id: LIVE_ACCOUNT,
  account_label: `Alpaca live · ${LIVE_ACCOUNT}`,
  evaluated_at_ms: NOW_MS,
} satisfies DeployBotView;

interface DeployLane {
  readonly view: DeployBotView;
  readonly before: AccountMoneyView;
  readonly withNew: AccountMoneyView;
  readonly amount: string;
  readonly bot: string;
}

const DEPLOY_LANES: Readonly<Record<'paper' | 'live', DeployLane>> = {
  paper: { view: PAPER_DEPLOY_VIEW, before: PAPER_MONEY_BEFORE, withNew: PAPER_MONEY_WITH_NEW, amount: PAPER_BUDGET, bot: WALKED_BOT },
  live: { view: LIVE_ACCOUNT_DEPLOY_VIEW, before: LIVE_MONEY, withNew: LIVE_MONEY_WITH_NEW, amount: LIVE_BUDGET, bot: LIVE_BOT },
};

/** The budget preview the backend authors for `body` on this lane: the
 * account as it stands with no amount, and with a NEW slice for the one
 * amount this walk types. Any other amount is a walk bug, answered as such. */
function budgetPreview(deploy: DeployLane, body: DeployBotBody): DeploymentBudgetPreview | null {
  const world = body.execution_mode === 'live' ? 'real_live' : 'real_paper';
  const amount = body.budget?.amount_usd ?? null;
  if (amount !== null && amount !== deploy.amount) return null;
  return {
    state: 'ready',
    detail: 'This is an entry-admission budget. Market fills and losses can exceed it.',
    world,
    custody_account_id: deploy.view.account_id,
    observed_at_ms: NOW_MS,
    risk_revision: 1,
    minimum_budget_usd: '500.00',
    unreserved_usd: deploy.before.free_to_deploy_usd,
    estimated_price_usd: '500.00',
    shortcuts: [{
      key: 'position_headroom',
      label: '1.2 × one position',
      amount_usd: '600.00',
      explanation: '1.2 × the $500.00 estimated position, including modelled fees. Market fills can cost more.',
    }],
    bot_name_note: BOT_NAME_NOTE,
    risk_limits_summary:
      'Daily loss limit: the smaller of 2% of prior-close equity and $500.00. Existing exit terms stay fixed.',
    money_after: amount === null ? deploy.before : deploy.withNew,
    review_token: amount === null ? null : `review-${world}-${amount}`,
    budget_usd: amount,
    confirmation_text: amount !== null && world === 'real_live' ? LIVE_PHRASE : null,
  };
}

function admission(deploy: DeployLane): RunAdmissionDecision {
  return {
    operation: 'START',
    allowed: true,
    reason_code: 'START_ADMITTED',
    explanation: 'The process slot is absent, market data is ready, and the Clerk proves flat custody.',
    next_step: null,
    strategy_instance_id: deploy.bot,
    proposed_run_id: `run-${deploy.bot}`,
    configuration_hash: 'a'.repeat(64),
    account_id: deploy.view.account_id,
    evaluated_at_ms: NOW_MS,
    fact_ages_ms: { runtime: 5, process: 10, market_data: 20, market_liveness: 20, clerk: 30, program_build: 15 },
    evidence_refs: ['walk-admission'],
  };
}

function receipt(deploy: DeployLane, world: BudgetDeployReceipt['world']): BudgetDeployReceipt {
  return {
    status: 'deployed',
    outcome: 'success',
    receipt_id: `command-${deploy.bot}`,
    command_id: `command-${deploy.bot}`,
    recorded_at_ms: NOW_MS,
    first_deployed_at_ms: NOW_MS,
    strategy_instance_id: deploy.bot,
    run_id: `run-${deploy.bot}`,
    account_id: deploy.view.account_id,
    world,
    committed_usd: deploy.amount,
    message: `${deploy.bot} is deployed`,
    explanation: `$${deploy.amount} is set aside for it.`,
    next_action: RECEIPT_NEXT_ACTION,
    replaces_strategy_instance_id: null,
  };
}

// ── The symbol picker's world: SPY is listed and held ──────────────────────

const VENDOR_CATALOG = [
  { symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCX', status: 'active', asset_class: 'us_equity' },
] satisfies SymbolCatalogEntry[];
const LAKE_COVERAGE = {
  market: 'usa',
  kinds: [],
  symbols: [{
    symbol: 'SPY',
    first_trading_date_ms: 1_704_205_800_000,
    last_trading_date_ms: 1_789_825_800_000,
    artifact_count: 680,
  } satisfies SymbolCoverageSpan],
};

// ── The walked bot, per state ──────────────────────────────────────────────

/** Where the walked bot is in its life. */
export type BotPhase = 'not_deployed' | 'running' | 'holding' | 'finished' | 'cleared';

const EARLIER_FINISHED = fakeCatalogBot({
  strategy_instance_id: EARLIER_BOT,
  account_id: PAPER_ACCOUNT,
  symbol: 'QQQ',
  phase: 'OFF_DUTY',
  desired_state: 'STOPPED',
  running: false,
  status_label: 'Off duty',
  status_explanation: FINISHED_EXPLANATION,
  fills_today: 0,
  realized_pnl_today: 0,
  open_pnl: null,
  last_activity_at_ms: NOW_MS - 3 * 86_400_000,
  group: 'finished',
  final_result_usd: '-3.10',
  trade_count: 4,
  ended_at_ms: NOW_MS - 3 * 86_400_000,
});

function walkedCatalogRow(phase: BotPhase): BotCatalogView | null {
  const base = {
    strategy_instance_id: WALKED_BOT,
    account_id: PAPER_ACCOUNT,
    symbol: 'SPY',
    last_activity_at_ms: NOW_MS,
    world_label: 'PAPER · practice money',
  };
  switch (phase) {
    case 'running':
      return fakeCatalogBot({
        ...base, status_explanation: RUNNING_EXPLANATION, exposure: { SPY: 1 },
        fills_today: 1, realized_pnl_today: 0, open_pnl: 0, group: 'running',
      });
    case 'holding':
      return fakeCatalogBot({
        ...base, phase: 'OFF_DUTY', desired_state: 'STOPPED', running: false, status_label: 'Off duty',
        status_explanation: HOLDING_EXPLANATION, exposure: { SPY: 1 },
        fills_today: 1, realized_pnl_today: 0, open_pnl: 0, group: 'holding',
      });
    case 'finished':
      return fakeCatalogBot({
        ...base, phase: 'OFF_DUTY', desired_state: 'STOPPED', running: false, status_label: 'Off duty',
        status_explanation: FINISHED_EXPLANATION, exposure: {},
        fills_today: 2, realized_pnl_today: 0.99, open_pnl: null, group: 'finished',
        final_result_usd: '0.99', trade_count: 2, ended_at_ms: NOW_MS,
      });
    case 'not_deployed':
    case 'cleared':
      return null;
  }
}

const STOP_ACTION = fakeSqliteStopAction({ concurrency_token: 'stop-running' });
const RECONCILE_ACTION = fakePanelAction('reconcile_now', {
  label: 'Reconcile now', explanation: 'Check this bot\'s position with Alpaca.', concurrency_token: 'reconcile-1',
});
const PREPARE_ACTION = fakePanelAction('prepare_safe_flatten', {
  label: 'Prepare safe flatten', explanation: 'Prepare a sale of exactly what this bot holds.',
  concurrency_token: 'prepare-after-reconcile',
});
const EXECUTE_ACTION = fakePanelAction('execute_safe_flatten', {
  label: 'Sell the prepared position', explanation: 'Send the prepared sale.', concurrency_token: 'execute-prepared',
});

const PANEL_PROFILE = {
  broker: 'alpaca',
  fee_fidelity: 'per_fill',
  live_bars_supported: true,
  stations: [],
  supported_action_ids: ['deploy', 'archive'],
} satisfies PanelProfile;

/** The flatten sequence's progress through the Clerk: reconcile mints the
 * evidence a prepared sale needs, and a checked plan is what may be sent. */
interface FlattenProgress {
  reconciled: boolean;
  checked: boolean;
}

// ── Recorded traffic ───────────────────────────────────────────────────────

/** One command the page sent, with its decoded body. */
export interface SentCommand {
  readonly method: string;
  readonly path: string;
  readonly body: unknown;
}

/**
 * Reads the walk leaves unanswered on purpose, each with why. They are the
 * only `/api/` requests allowed to reach the 503 fallback; anything else the
 * page asks for is reported by {@link OwnerWalkWorld.unexpected}.
 */
const UNANSWERED_ON_PURPOSE: readonly { readonly pattern: RegExp; readonly why: string }[] = [
  {
    // The chart is not what this walk is about; the page says the history
    // could not be read and keeps every control working.
    pattern: /\/bots\/[^/]+\/chart\/history$/,
    why: 'the bot page\'s chart history',
  },
  {
    pattern: /\/bots\/[^/]+\/runs\/(current|history)$/,
    why: 'the bot page\'s run timing, which says it could not be loaded',
  },
  {
    pattern: /^\/api\/(dataset\/available|chart\/indicators\/supported)$/,
    why: 'the bot chart\'s indicator menu',
  },
];

/**
 * The mocked backend. `install()` routes every request; read `sent` and
 * `unexpected()` to assert what the page asked for.
 */
export class OwnerWalkWorld {
  /** Every POST/PUT/PATCH the page sent, in order. */
  readonly sent: SentCommand[] = [];
  /** Every `/api/` request answered by the 503 fallback. */
  readonly unanswered: string[] = [];
  /** Where the walked bot is. */
  phase: BotPhase = 'not_deployed';
  private flatten: FlattenProgress = { reconciled: false, checked: false };
  /** Bumped on every state change, so the bot page adopts the new snapshot. */
  private surfaceVersion = 1;
  /** The sids the last clear took off Home. */
  private readonly cleared = new Set<string>();
  /** Commands held back until the walk releases them, by `action_id`. */
  private readonly holds = new Map<string, Promise<void>>();

  /** Requests the page made that the walk neither answers nor expects. */
  unexpected(): string[] {
    return this.unanswered.filter((entry) => !UNANSWERED_ON_PURPOSE.some(({ pattern }) => pattern.test(entry.split(' ')[1] ?? '')));
  }

  /** The commands sent to one path suffix, decoded. */
  commandsTo(suffix: string): SentCommand[] {
    return this.sent.filter((command) => command.path.endsWith(suffix));
  }

  /**
   * Hold the next command carrying `actionId` until the returned release is
   * called, so the walk can read the page while that command is in flight.
   * Nothing about the world changes until it is released.
   */
  hold(actionId: string): () => void {
    let release = (): void => undefined;
    this.holds.set(actionId, new Promise<void>((resolve) => { release = resolve; }));
    return () => release();
  }

  async install(page: Page): Promise<void> {
    await page.route('**/*', (route) => this.answer(route));
  }

  private advance(phase: BotPhase): void {
    this.phase = phase;
    this.surfaceVersion += 1;
  }

  private async answer(route: Route): Promise<void> {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) {
      await route.continue();
      return;
    }
    const method = request.method();
    if (method !== 'GET') {
      const body = request.postDataJSON() as unknown;
      this.sent.push({ method, path, body });
      const actionId = (body as { action_id?: unknown } | null)?.action_id;
      const held = typeof actionId === 'string' ? this.holds.get(actionId) : undefined;
      if (held !== undefined) {
        this.holds.delete(actionId as string);
        await held;
      }
    }
    const answer = this.route(method, path, request);
    if (answer === null) {
      this.unanswered.push(`${method} ${path}`);
      await route.fulfill({ status: 503, json: { detail: 'Outside the owner walk-through.' } });
      return;
    }
    if (answer.kind === 'stream') {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache' },
        body: '',
      });
      return;
    }
    await route.fulfill({ status: answer.status ?? 200, json: answer.json });
  }

  /** The answer for one request, or `null` for the 503 fallback. */
  private route(
    method: string,
    path: string,
    request: Request,
  ): { kind: 'json'; json: unknown; status?: number } | { kind: 'stream' } | null {
    const json = (value: unknown, status?: number) => ({ kind: 'json' as const, json: value, status });

    // The shell: the fleet, each lane's verdict and attention.
    if (path === '/api/broker-clerks') return json(DIRECTORY);
    if (path === '/api/broker-clerks/aggregate/attention') return json(this.attention());
    if (path === `${CLERK_SCOPE(PAPER_CLERK)}/live-verdict`) return json(verdict('paper', PAPER_ACCOUNT));
    if (path === `${CLERK_SCOPE(LIVE_CLERK)}/live-verdict`) return json(verdict('live', LIVE_ACCOUNT));
    if (path === '/api/brokers/alpaca/panel-profile') return json(PANEL_PROFILE);
    // The symbol picker on Deploy: SPY listed, held by the lake, no jobs.
    if (path === '/api/tickers/catalog') return json(VENDOR_CATALOG);
    if (path === '/api/data-lake/storage-summary') return json(LAKE_COVERAGE);
    if (path === '/api/jobs') return json([]);

    // Each lane's header reads.
    if (path === `${CLERK_SCOPE(PAPER_CLERK)}/account`) return json(brokerAccount(PAPER_ACCOUNT, 'paper', 25_000));
    if (path === `${CLERK_SCOPE(LIVE_CLERK)}/account`) return json(brokerAccount(LIVE_ACCOUNT, 'live', 5_000));
    if (path === `${CLERK_SCOPE(PAPER_CLERK)}/clerk/status`) return json(clerkStatus(PAPER_ACCOUNT, 'real_paper'));
    if (path === `${CLERK_SCOPE(LIVE_CLERK)}/clerk/status`) return json(clerkStatus(LIVE_ACCOUNT, 'real_live'));

    // Paper: Home.
    if (path === `${PAPER}/money`) return json(this.paperMoney());
    if (path === `${PAPER}/bots/catalog`) return json(this.paperCatalog());
    // Live: Home, empty.
    if (path === `${LIVE}/money`) return json(LIVE_MONEY);
    if (path === `${LIVE}/bots/catalog`) return json([] satisfies BotCatalogView[]);

    // Deploy, on either lane.
    for (const [scope, deploy] of [[PAPER, DEPLOY_LANES.paper], [LIVE, DEPLOY_LANES.live]] as const) {
      if (method === 'GET' && path === `${scope}/bots/deploy`) return json(deploy.view);
      if (method === 'POST' && path === `${scope}/bots/budget-preview`) {
        const preview = budgetPreview(deploy, request.postDataJSON() as DeployBotBody);
        return preview === null ? null : json(preview);
      }
      if (method === 'POST' && path === `${scope}/bots/admission`) return json(admission(deploy));
      if (method === 'POST' && path === `${scope}/bots`) {
        const body = request.postDataJSON() as Enveloped<DeploySubmissionBody>;
        if (deploy === DEPLOY_LANES.paper) this.advance('running');
        return json(receipt(deploy, body.execution_mode === 'live' ? 'real_live' : 'real_paper'));
      }
    }

    // Paper: the walked bot's page and its commands.
    const bot = `${PAPER}/bots/${WALKED_BOT}`;
    if (path === `${bot}/panel`) return json(this.panel());
    if (path === `${bot}/live-snapshot`) return json(this.snapshot());
    if (path === `${bot}/live-stream`) return { kind: 'stream' };
    if (path === `${bot}/budget`) return json(this.botBudget());
    if (method === 'POST' && path === `${bot}/actions/quiesce`) {
      const result = this.runAction(request.postDataJSON() as Enveloped<PanelActionRequest>);
      return result === null ? null : json(result);
    }
    if (method === 'POST' && path === `${PAPER}/custody/bots/${WALKED_BOT}/recovery-actions/check`) {
      const check = this.checkPlan(request.postDataJSON() as RecoveryActionCheckRequest);
      return check === null ? null : json(check);
    }
    if (method === 'POST' && path === `${PAPER}/bots/clear`) {
      return json(this.clear(request.postDataJSON() as Enveloped<BotClearRequest>));
    }
    return null;
  }

  private attention(): AggregateAttentionResponse {
    const items: LaneAttentionItem[] = this.phase === 'holding'
      ? [{
          condition_id: `stopped-holding:${WALKED_BOT}`,
          reason_code: 'STOPPED_STILL_HOLDING',
          kind: 'stopped_holding',
          severity: 'warning',
          strategy_instance_id: WALKED_BOT,
          symbol: 'SPY',
          headline: HOLDING_ATTENTION,
          action: { label: 'Flatten…', destination: 'bot' },
        }]
      : [];
    return {
      observed_at_ms: NOW_MS,
      lanes: [
        { broker: 'alpaca', clerk_id: PAPER_CLERK, ok: true, value: { account_id: PAPER_ACCOUNT, items } },
        { broker: 'alpaca', clerk_id: LIVE_CLERK, ok: true, value: { account_id: LIVE_ACCOUNT, items: [] } },
      ],
    };
  }

  private paperMoney(): AccountMoneyView {
    switch (this.phase) {
      case 'not_deployed': return PAPER_MONEY_BEFORE;
      case 'running': return PAPER_MONEY_RUNNING;
      case 'holding': return PAPER_MONEY_HOLDING;
      case 'finished':
      case 'cleared': return PAPER_MONEY_FINISHED;
    }
  }

  private paperCatalog(): BotCatalogView[] {
    const walked = walkedCatalogRow(this.phase);
    return [...(walked === null ? [] : [walked]), EARLIER_FINISHED]
      .filter((row) => !this.cleared.has(row.strategy_instance_id));
  }

  private panel(): BotPanelView {
    const running = this.phase === 'running';
    const holding = this.phase === 'holding';
    const actions: PanelAction[] = running
      ? [STOP_ACTION]
      : holding
        ? [
            RECONCILE_ACTION,
            ...(this.flatten.reconciled ? [PREPARE_ACTION] : []),
            ...(this.flatten.checked ? [EXECUTE_ACTION] : []),
          ]
        : [];
    return fakeBotPanelView({
      strategy_instance_id: WALKED_BOT,
      account_id: PAPER_ACCOUNT,
      symbol: 'SPY',
      updated_at_ms: NOW_MS,
      revision: this.surfaceVersion,
      health: {
        strategy_instance_id: WALKED_BOT,
        phase: running ? 'ON_DUTY' : 'OFF_DUTY',
        phase_label: running ? 'On duty' : 'Off duty',
        desired_state: running ? 'RUNNING' : 'STOPPED',
        desired_state_label: running ? 'Running' : 'Stopped',
        running,
        duty_outcome: running ? null : {
          kind: 'STOPPED',
          label: 'Stopped',
          explanation: 'Stopped by the owner from Home.',
          reason_code: 'OWNER_STOP',
          recorded_at_ms: NOW_MS,
          run_id: `run-${WALKED_BOT}`,
        },
        last_decision_at_ms: NOW_MS,
        decision_stale: false,
        last_bar_at_ms: NOW_MS,
      },
      actions,
      mission_verdict: running
        ? { state: 'working', label: 'Working', explanation: 'The runtime is on duty.', next_action: 'Monitor decisions.', evaluated_at_ms: NOW_MS }
        : { state: 'off_duty', label: 'Off duty', explanation: 'The bot is stopped.', next_action: null, evaluated_at_ms: NOW_MS },
      primary_action: running ? 'stop_bot_decisions' : null,
      status: running ? 'running' : holding ? 'holding' : 'finished',
      exposure: running || holding ? { SPY: 1 } : {},
      fills_today: running || holding ? 1 : 2,
    });
  }

  private snapshot(): BotPanelLiveSnapshot {
    return {
      stream_epoch: 'owner-walk',
      surface_version: this.surfaceVersion,
      panel: this.panel(),
      live_chart: {
        as_of_ms: NOW_MS,
        bars: [],
        feed: fakeChartFeed(),
        fill_markers: [],
        overlay_notices: [],
        resolution: '5s',
        strategy_instance_id: WALKED_BOT,
        symbol: 'SPY',
        trading_date_open_ms: NOW_MS - 3_600_000,
        trading_date_close_ms: NOW_MS + 19_800_000,
      },
    };
  }

  private botBudget(): DeploymentBudgetView {
    const money = this.paperMoney();
    const segment = money.segments?.find((candidate) => candidate.strategy_instance_id === WALKED_BOT) ?? null;
    return {
      state: 'ready',
      strategy_instance_id: WALKED_BOT,
      world: 'real_paper',
      headline: segment === null ? 'All of this bot\'s money is back in free to deploy.' : `This bot's money: $${segment.amount_usd}.`,
      detail: 'Budget, plus realized results, minus fees.',
      committed_usd: PAPER_BUDGET,
      observed_at_ms: NOW_MS,
      segment,
      statement: [
        { label: 'Budget', amount_usd: PAPER_BUDGET },
        { label: 'Balance', amount_usd: segment?.amount_usd ?? '0.00', total: true },
      ],
    };
  }

  /** Stop, and the flatten sequence's reconcile and sale — each only in the
   * state the page was shown it in. Anything else is a walk bug: 503. */
  private runAction(request: Enveloped<PanelActionRequest>): PanelActionResult | null {
    const result = (action: PanelAction, message: string): PanelActionResult => ({
      action_id: action.action_id,
      applied: true,
      concurrency_token: action.concurrency_token,
      message,
      outcome: 'success',
      receipt_id: request.idempotency_key,
      recorded_at_ms: NOW_MS,
      revision: this.surfaceVersion,
    });
    if (request.action_id === 'stop_bot_decisions' && this.phase === 'running') {
      this.advance('holding');
      return result(STOP_ACTION, STOP_MESSAGE);
    }
    if (request.action_id === 'reconcile_now' && this.phase === 'holding') {
      this.flatten = { ...this.flatten, reconciled: true };
      this.surfaceVersion += 1;
      return result(RECONCILE_ACTION, RECONCILE_MESSAGE);
    }
    if (request.action_id === 'execute_safe_flatten' && this.phase === 'holding' && this.flatten.checked) {
      this.advance('finished');
      return result(EXECUTE_ACTION, SELL_MESSAGE);
    }
    return null;
  }

  /** The prepared sale: exactly the 1 SPY this bot holds, at market. */
  private checkPlan(request: RecoveryActionCheckRequest): SqliteRecoveryActionCheck | null {
    if (request.action_id !== 'prepare_safe_flatten' || !this.flatten.reconciled || this.phase !== 'holding') return null;
    this.flatten = { ...this.flatten, checked: true };
    this.surfaceVersion += 1;
    return {
      capability: {
        action_id: 'prepare_safe_flatten',
        available: true,
        concurrency_token: PREPARE_ACTION.concurrency_token,
        confirmation: null,
        evidence: [],
        execution_ref: null,
        explanation: 'Sell exactly the shares this bot holds.',
        freshness: 'fresh',
        label: 'Prepare safe flatten',
        mutation: false,
        next_step: PLAN_NEXT_STEP,
        primary: true,
        reduction_plan: {
          account_id: PAPER_ACCOUNT,
          authority_generation: BINDING_GENERATION,
          control_revision: 7,
          db_identity_token: 'db-owner-walk',
          expires_at_ms: NOW_MS + 30_000,
          legs: [{
            position_updated_at_ms: NOW_MS,
            quantity: 1,
            side: 'sell',
            strategy_instance_id: WALKED_BOT,
            symbol: 'SPY',
          }],
          prepared_at_ms: NOW_MS,
          reconciliation_id: 'reconciliation:42',
          scope: 'CUSTODY_SUBJECT',
          strategy_instance_id: WALKED_BOT,
          version_token: 'plan-owner-walk',
        },
        scope: 'CUSTODY_SUBJECT',
        unavailable_reason: null,
        unavailable_reason_code: null,
      },
      reduction_pricing: { kind: 'regular_session' },
    };
  }

  /** One archive leg per named bot, answered in request order: the walked
   * bot clears; the earlier one is refused with the guard's own words. */
  private clear(request: Enveloped<BotClearRequest>): CohortActionResult {
    const legs = request.strategy_instance_ids.map((sid): CohortLegResult => {
      if (sid === EARLIER_BOT) {
        return {
          strategy_instance_id: sid,
          outcome: 'refused',
          result: null,
          error: { action_id: 'archive', outcome: 'conflict', receipt_id: null, recorded_at_ms: NOW_MS, ...CLEAR_REFUSAL },
        };
      }
      this.cleared.add(sid);
      if (sid === WALKED_BOT) this.advance('cleared');
      return {
        strategy_instance_id: sid,
        outcome: 'applied',
        error: null,
        result: {
          action_id: 'archive',
          applied: true,
          concurrency_token: `archive-${sid}`,
          message: ARCHIVE_MESSAGE,
          outcome: 'success',
          receipt_id: `${request.idempotency_key}:${sid}`,
          recorded_at_ms: NOW_MS,
          revision: this.surfaceVersion,
        },
      };
    });
    const count = (outcome: CohortLegResult['outcome']) => legs.filter((leg) => leg.outcome === outcome).length;
    return {
      account_id: PAPER_ACCOUNT,
      receipt_id: request.idempotency_key,
      recorded_at_ms: NOW_MS,
      legs,
      applied_count: count('applied'),
      replayed_count: count('replayed'),
      refused_count: count('refused'),
      failed_count: count('failed'),
    };
  }
}
