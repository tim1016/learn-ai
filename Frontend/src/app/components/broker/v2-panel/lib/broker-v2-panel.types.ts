/**
 * Frontend TypeScript types mirroring the S1 Python schema contracts.
 *
 * All temporal fields are `int64 ms UTC` numbers per temporal-rigor.md.
 * These types are the single Frontend source of truth for the broker-v2
 * panel surface; nothing invents display strings here — that is the
 * backend's job (backend-authored copy authority, spec §13).
 */

// ── Action ids (closed vocabulary, spec §11) ─────────────────────────────────

export type ActionId =
  | 'deploy'
  | 'start'
  | 'stop'
  | 'flatten_stop'
  | 'retire'
  | 'cancel_order'
  | 'clear_hold'
  | 'reconcile_now';

// ── Operator-blocker reuse (OperatorBlocker contract) ────────────────────────

export interface OperatorBlocker {
  readonly code: string;
  readonly label: string;
  readonly explanation: string;
  readonly disposition: 'fix_here' | 'fix_elsewhere' | 'wait' | 'terminal';
  readonly action_hint: string | null;
}

export interface OperatorConfirmationCopy {
  readonly required: boolean;
  readonly prompt: string;
  readonly ack_phrase: string | null;
}

// ── §4 Panel profile ─────────────────────────────────────────────────────────

export interface StationApplicability {
  readonly station_id: string;
  readonly applicable: boolean;
  readonly label: string;
  readonly explanation: string;
}

export interface PanelProfile {
  readonly broker: string;
  readonly fee_fidelity: 'per_fill' | 'aggregate' | 'none';
  readonly flatten_supported: boolean;
  readonly live_bars_supported: boolean;
  readonly stations: readonly StationApplicability[];
  readonly supported_action_ids: readonly ActionId[];
}

// ── §5 Catalog view ──────────────────────────────────────────────────────────

export interface BotCatalogView {
  readonly strategy_instance_id: string;
  readonly broker: string;
  readonly account_id: string;
  readonly symbol: string;
  readonly phase: string;
  readonly desired_state: string;
  readonly running: boolean;
  readonly status_label: string;
  readonly exposure: Record<string, number>;
  readonly fills_today: number;
  readonly realized_pnl_today: number;
  readonly open_pnl: number | null;
  readonly last_activity_at_ms: number | null;
  readonly needs_attention: boolean;
}

// ── §7 Panel view ────────────────────────────────────────────────────────────

export interface DutyOutcomeView {
  readonly kind: string;
  readonly reason_code: string;
  readonly label: string;
  readonly explanation: string;
  readonly recorded_at_ms: number | null;
  readonly run_id: string | null;
}

export interface BotHealthCard {
  readonly strategy_instance_id: string;
  readonly phase: string;
  readonly phase_label: string;
  readonly desired_state: 'RUNNING' | 'STOPPED';
  readonly desired_state_label: string;
  readonly running: boolean;
  readonly duty_outcome: DutyOutcomeView | null;
  readonly last_decision_at_ms: number | null;
  readonly decision_stale: boolean;
  readonly last_bar_at_ms: number | null;
}

export interface ChannelHealthView {
  readonly stream: 'market_data' | 'execution';
  readonly state: string;
  readonly label: string;
  readonly explanation: string;
  readonly reason: string;
  readonly observed_at_ms: number;
}

export interface ClerkCard {
  readonly account_id: string;
  readonly hold_active: boolean;
  readonly hold_reason: string;
  readonly hold_reason_label: string;
  readonly hold_reason_explanation: string;
  readonly hold_since_ms: number | null;
  readonly reconciliation_verdict: string | null;
  readonly reconciliation_verdict_label: string | null;
  readonly last_sweep_at_ms: number | null;
  readonly outstanding_intents: number;
  readonly channels: readonly ChannelHealthView[];
}

export interface StationView {
  readonly station_id: string;
  readonly label: string;
  readonly state: string;
  readonly state_label: string;
  readonly receipt: string;
  readonly evidence_at_ms: number | null;
  readonly blocker: OperatorBlocker | null;
}

export interface TransactionRail {
  readonly transaction_ref: string | null;
  readonly stations: readonly StationView[];
}

export interface PanelAction {
  readonly action_id: ActionId;
  readonly label: string;
  readonly explanation: string;
  readonly enabled: boolean;
  readonly blockers: readonly OperatorBlocker[];
  readonly confirmation: OperatorConfirmationCopy | null;
  readonly revision: number;
}

export interface BotPanelView {
  readonly strategy_instance_id: string;
  readonly broker: string;
  readonly account_id: string;
  readonly symbol: string;
  readonly mode: 'log_only';
  readonly revision: number;
  readonly health: BotHealthCard;
  readonly clerk: ClerkCard;
  readonly rail: TransactionRail;
  readonly journal_tail_ref: string;
  readonly journal_tail_seq: number | null;
  readonly actions: readonly PanelAction[];
}

// ── §11 Action execution ─────────────────────────────────────────────────────

export interface PanelActionRequest {
  readonly action_id: ActionId;
  readonly revision: number;
  readonly idempotency_key: string;
  readonly reason: string | null;
}

export interface PanelActionResult {
  readonly action_id: ActionId;
  readonly applied: boolean;
  readonly revision: number;
  readonly message: string;
}

// ── §8 Chart types ───────────────────────────────────────────────────────────

export type ChartSource = 'ibkr' | 'polygon' | 'mixed';
export type ChartHistoryPreset = '1D' | '5D' | '1M' | '3M' | '1Y' | 'All';
export type ChartAggregation = '1m' | '5m' | '30m' | '1h' | '1d';

export interface ChartBar {
  readonly start_ms: number;
  readonly end_ms: number;
  readonly open: string;
  readonly high: string;
  readonly low: string;
  readonly close: string;
  readonly volume: number;
  readonly source: ChartSource;
}

export interface ChartFillMarker {
  readonly filled_at_ms: number;
  readonly side: 'buy' | 'sell';
  readonly quantity: number;
  readonly price: number;
  readonly order_ref: string;
}

export interface ChartOverlayNoticeView {
  readonly code: string;
  readonly message: string;
  readonly source: 'polygon';
}

export interface ChartLiveResponse {
  readonly strategy_instance_id: string;
  readonly symbol: string;
  readonly trading_date_open_ms: number;
  readonly trading_date_close_ms: number;
  readonly resolution: '5s' | '1m';
  readonly bars: readonly ChartBar[];
  readonly fill_markers: readonly ChartFillMarker[];
  readonly overlay_notices: readonly ChartOverlayNoticeView[];
  readonly as_of_ms: number;
}

export interface ChartHistoryResponse {
  readonly strategy_instance_id: string;
  readonly symbol: string;
  readonly preset: ChartHistoryPreset;
  readonly aggregation: ChartAggregation;
  readonly from_ms: number;
  readonly to_ms: number;
  readonly bars: readonly ChartBar[];
  readonly fill_markers: readonly ChartFillMarker[];
  readonly truncated: boolean;
  readonly as_of_ms: number;
}

// ── §14 Operator-gated evidence ──────────────────────────────────────────────

export interface EvidenceEntry {
  readonly seq: number;
  readonly kind: string;
  readonly kind_label: string;
  readonly recorded_at_ms: number;
  readonly order_ref: string | null;
  readonly intent_id: string | null;
  readonly summary: string;
  readonly has_more_detail: boolean;
}

export interface EvidencePage {
  readonly strategy_instance_id: string;
  readonly account_id: string;
  readonly transaction_ref: string | null;
  readonly entries: readonly EvidenceEntry[];
  readonly next_cursor: number | null;
  readonly total_entries: number;
  readonly truncated: boolean;
  readonly read_by: string;
  readonly read_at_ms: number;
}
