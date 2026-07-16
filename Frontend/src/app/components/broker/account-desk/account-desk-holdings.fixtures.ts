import type {
  AccountTruthFactOwner,
  AccountTruthPositionRow,
  AccountTruthResponse,
  IbkrAccountSummary,
  IbkrPosition,
  IbkrPositionsSnapshot,
} from '../../../api/broker-models';
import type { OperatorBlocker } from '../../../api/operator-blocker.types';

export function makeAccountSummary(accountId = 'DU1234567'): IbkrAccountSummary {
  return {
    account_id: accountId,
    is_paper: true,
    base_currency: 'USD',
    cash_balance: 1_000,
    net_liquidation: 10_000,
    buying_power: 20_000,
    day_pnl: 25,
    fetched_at_ms: 1_780_000_002_000,
  };
}

export function makePosition(accountId = 'DU1234567', conId = 12345): IbkrPosition {
  return {
    account_id: accountId,
    con_id: conId,
    symbol: 'SPY',
    sec_type: 'STK',
    currency: 'USD',
    multiplier: 1,
    quantity: 2,
    avg_cost: 500,
    market_price: 505,
    market_value: 1_010,
    fetched_at_ms: 1_780_000_002_000,
  };
}

export function makePositionsSnapshot(
  accountId = 'DU1234567',
  positions: IbkrPosition[] = [makePosition(accountId)],
): IbkrPositionsSnapshot {
  return {
    account_id: accountId,
    is_paper: true,
    positions,
    fetched_at_ms: 1_780_000_002_000,
    used_cache_fallback: false,
  };
}

export function makePositionOwner(
  overrides: Partial<AccountTruthFactOwner> = {},
): AccountTruthFactOwner {
  return {
    owner_class: 'bot',
    owner_key: 'bot-a',
    owner_label: 'Bot alpha',
    evidence_tier: 'bot_order_ref',
    evidence_label: 'Bot order reference',
    owner_binding_state: 'ACTIVE',
    severity: 'ok',
    ...overrides,
  };
}

export function makeTruthPosition(
  position: IbkrPosition,
  owner: AccountTruthFactOwner = makePositionOwner(),
): AccountTruthPositionRow {
  return {
    fact_kind: 'position',
    account_id: position.account_id,
    con_id: position.con_id,
    symbol: position.symbol,
    sec_type: position.sec_type,
    quantity: position.quantity,
    avg_cost: position.avg_cost,
    market_value: position.market_value ?? null,
    owner,
    headline: 'Position ownership is attested',
    detail: 'Account Truth assigned this holding to its backend-owned owner.',
    fetched_at_ms: position.fetched_at_ms,
  };
}

export function makeAccountTruth(
  accountId = 'DU1234567',
  positions: AccountTruthPositionRow[] = [makeTruthPosition(makePosition(accountId))],
  operatorBlockers: OperatorBlocker[] = [],
): AccountTruthResponse {
  return {
    account_id: accountId,
    final_verdict: 'clean',
    final_severity: 'ok',
    status_label: 'Clean',
    status_detail: 'Required live broker evidence is assigned to known ownership.',
    generated_at_ms: 1_780_000_002_000,
    health: {
      mode: 'paper',
      host: '127.0.0.1',
      port: 4002,
      client_id: 7,
      connected: true,
      disabled: false,
      reason: null,
      account_id: accountId,
      is_paper: true,
      server_version: 178,
      fetched_at_ms: 1_780_000_002_000,
      safety_verdict: {
        configured_mode: 'paper',
        readonly_flag: false,
        port_class: 'paper_port',
        connected_account_prefix: 'DU',
        final_verdict: 'paper-only',
        failing_gates: [],
        unknown_gates: [],
      },
      connection_state: 'connected',
      last_transition_ms: 1_780_000_002_000,
      connection_lost: false,
      connectivity_lost_count: 0,
      reconnect_attempt: null,
    },
    account: null,
    known_bot_namespaces: [],
    manual_namespaces_observed: [],
    invariants: [],
    blockers: [],
    operator_blockers: operatorBlockers,
    caveats: [],
    owner_summaries: [],
    symbol_exposures: [],
    orders: [],
    executions: [],
    positions,
    evidence_gaps: [],
    source_freshness: [],
  };
}

export function makeUnattributedHoldingBlocker(conId = 12345): OperatorBlocker {
  return {
    condition: { id: 'unattributed_holding', severity: 'blocking', scope: 'account', evidence: { con_id: conId } },
    host: 'account_desk',
    anchor: { kind: 'holdings_row', subject_key: String(conId) },
    audience: 'both',
    disposition: 'fix_elsewhere',
    headline: 'Foreign or unclaimed broker position',
    detail: 'Backend-authored ownership evidence is required before this holding can be treated as managed.',
    primary_move: {
      label: 'Open IBKR setup guide',
      action: { kind: 'navigate', route: '/docs/ibkr-setup-guide', fragment: null },
      target: null,
    },
    secondary_moves: [],
    applies_to: 'both',
  };
}
