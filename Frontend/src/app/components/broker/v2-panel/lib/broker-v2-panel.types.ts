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

export type ActionId = components['schemas']['PanelAction']['action_id'];

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
export type PrimaryActionByLens = components['schemas']['PrimaryActionByLens'];

/**
 * `authority_kind` names the exact Clerk account authority (real Paper vs
 * one isolated Dry Run `sim:` account) this row was read from — never both
 * at once (issue #1729 AC #8).
 */
export type RecentDecisionView = components['schemas']['RecentDecisionView'];
export type RecentFillView = components['schemas']['RecentFillView'];

/**
 * One `PanelActionButtonComponent` trigger event. The reason remains nullable
 * in the transport contract; current presented actions do not collect one.
 */
export interface PanelActionTrigger {
  readonly action: PanelAction;
  readonly reason: string | null;
}

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

export type ChartSource = components['schemas']['ChartBar']['source'];
export type ChartLiveResolution = components['schemas']['ChartLiveResponse']['resolution'];
export type ChartHistoryTimeframe =
  components['schemas']['ChartHistoryResponse']['timeframe'];

export type ChartBar = components['schemas']['ChartBar'];

/**
 * `event_key` is the stable per-fill identity — distinct from `order_ref`,
 * which every partial fill of one order shares. Consumers distinguishing
 * individual fills (an incremental cursor, a merge across partial fills)
 * must key on it.
 */
export type ChartFillMarker = components['schemas']['ChartFillMarker'];

export type ChartOverlayNoticeView = components['schemas']['ChartOverlayNoticeView'];
export type ChartLiveResponse = components['schemas']['ChartLiveResponse'];
export type ChartHistoryResponse = components['schemas']['ChartHistoryResponse'];

export type BotPanelLiveSnapshot = components['schemas']['BotPanelLiveSnapshot'];

// ── §14 Operator-gated evidence ──────────────────────────────────────────────

export type EvidenceEntry = components['schemas']['EvidenceEntry'];
export type EvidencePage = components['schemas']['EvidencePage'];
