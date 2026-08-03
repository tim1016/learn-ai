/**
 * Frontend aliases over mechanically generated Python OpenAPI contracts.
 *
 * All temporal fields are `int64 ms UTC` numbers per temporal-rigor.md.
 * Python owns the semantic contract; this file adds only convenient local
 * names plus closed rendering-only unions for template exhaustiveness.
 */

import type { components } from '../../../../api/broker.types';

// ── Channel health state (closed vocabulary) ─────────────────────────────────

export type ChannelState = 'healthy' | 'unhealthy' | 'unknown';
export type StationState =
  | 'satisfied'
  | 'waiting'
  | 'blocked'
  | 'unknown_stale'
  | 'not_applicable';

// ── Action ids (closed vocabulary, spec §11) ─────────────────────────────────

export type ActionId =
  | 'deploy'
  | 'resume'
  | 'pause'
  | 'continue'
  | 'stop'
  | 'flatten_stop'
  | 'retire'
  | 'cancel_order'
  | 'clear_hold'
  | 'record_inventory_baseline'
  | 'reconcile_now';

// ── Operator-blocker reuse (OperatorBlocker contract) ────────────────────────

export type OperatorBlocker = components['schemas']['OperatorBlocker'];
export type OperatorConfirmationCopy =
  components['schemas']['OperatorConfirmationCopy'];

// ── §4 Panel profile ─────────────────────────────────────────────────────────

export type StationApplicability =
  components['schemas']['StationApplicability'];
export type PanelProfile = components['schemas']['PanelProfile'];

// ── §5 Catalog view ──────────────────────────────────────────────────────────

export type BotCatalogView = components['schemas']['BotCatalogView'];

// ── §7 Panel view ────────────────────────────────────────────────────────────

export type DutyOutcomeView = components['schemas']['DutyOutcomeView'];
export type BotHealthCard = components['schemas']['BotHealthCard'];
export type ChannelHealthView = components['schemas']['ChannelHealthView'];
export type ClerkCard = components['schemas']['ClerkCard'];
export type StationView = components['schemas']['StationView'];
export type TransactionRail = components['schemas']['TransactionRail'];
export type PanelAction = components['schemas']['PanelAction'];
export type BotPanelView = components['schemas']['BotPanelView'];
export type MarketPulseView = components['schemas']['MarketPulseView'];

// ── Run navigation ──────────────────────────────────────────────────────────

export type BotRunView = components['schemas']['BotRunView'];
export type BotRunHistoryPage = components['schemas']['BotRunHistoryPage'];
export type RunHistoryMode = 'current' | 'history';
export type RunHistoryNavigation =
  | 'current'
  | 'history'
  | 'newer'
  | 'older';

export interface RunHistoryState {
  readonly mode: RunHistoryMode;
  readonly current: BotRunView | null;
  readonly history: BotRunHistoryPage | null;
  readonly currentLoading: boolean;
  readonly historyLoading: boolean;
  readonly currentFailed: boolean;
  readonly historyFailed: boolean;
  readonly canViewNewer: boolean;
}

export const EMPTY_RUN_HISTORY_STATE: RunHistoryState = Object.freeze({
  mode: 'current',
  current: null,
  history: null,
  currentLoading: false,
  historyLoading: false,
  currentFailed: false,
  historyFailed: false,
  canViewNewer: false,
});

// ── §11 Action execution ─────────────────────────────────────────────────────

export type PanelActionRequest = components['schemas']['PanelActionRequest'];
export type PanelActionResult = components['schemas']['PanelActionResult'];

// ── §8 Chart types ───────────────────────────────────────────────────────────

export type ChartSource = 'ibkr' | 'polygon' | 'mixed';
export type ChartLiveResolution = '5s' | '1m';
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
  readonly resolution: ChartLiveResolution;
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

type GeneratedBotPanelLiveSnapshot = components['schemas']['BotPanelLiveSnapshot'];
export type BotPanelLiveSnapshot = Omit<
  GeneratedBotPanelLiveSnapshot,
  'stream_epoch' | 'surface_version' | 'panel' | 'live_chart'
> & {
  readonly stream_epoch: string;
  readonly surface_version: number;
  readonly panel: BotPanelView;
  readonly live_chart: ChartLiveResponse;
};

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
