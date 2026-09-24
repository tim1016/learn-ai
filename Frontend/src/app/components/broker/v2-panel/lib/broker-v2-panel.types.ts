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

// ── Cohort-scoped affordances (ADR 0051 flatten, ADR 0052 archive) ──────────

export type CohortFlattenView = components['schemas']['CohortFlattenView'];
export type CohortFlattenCohort = components['schemas']['CohortFlattenCohort'];
export type CohortFlattenLeg = components['schemas']['CohortFlattenLeg'];
export type CohortFlattenRequest = components['schemas']['CohortFlattenRequest'];
export type CohortArchiveView = components['schemas']['CohortArchiveView'];
export type CohortArchiveCohort = components['schemas']['CohortArchiveCohort'];
export type CohortArchiveLeg = components['schemas']['CohortArchiveLeg'];
export type CohortArchiveRequest = components['schemas']['CohortArchiveRequest'];
/** The batch outcome both cohort actions report. */
export type CohortActionResult = components['schemas']['CohortActionResult'];
export type CohortLegResult = components['schemas']['CohortLegResult'];

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
export type FeedContinuityEventView = components['schemas']['FeedContinuityEventView'];
export type FeedContinuityView = components['schemas']['FeedContinuityView'];
export type ClerkCard = components['schemas']['ClerkCard'];
export type StationView = components['schemas']['StationView'];
export type ReadinessCheckView = components['schemas']['ReadinessCheckView'];
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

/**
 * Keeps the panel usable during a rolling clerk/frontend deployment. Older
 * clerks omit this newly introduced projection until they are restarted.
 */
export const FEED_CONTINUITY_NOT_RECORDED: FeedContinuityView = Object.freeze({
  provider_label: 'IBKR market data',
  state: 'not_recorded',
  state_label: 'Continuity not recorded',
  explanation: 'This clerk has not reported current-run feed continuity yet.',
  run_id: null,
  interruption_count: 0,
  recovery_count: 0,
  unresolved_count: 0,
  decision_impact_count: 0,
  last_interruption_at_ms: null,
  last_recovery_at_ms: null,
  latest_bar_at_ms: null,
  events: [],
});

export function feedContinuityFor(panel: BotPanelView): FeedContinuityView {
  return panel.feed_continuity ?? FEED_CONTINUITY_NOT_RECORDED;
}

// ── Run navigation ──────────────────────────────────────────────────────────

export type BotRunView = components['schemas']['BotRunView'];
export type BotRunHistoryPage = components['schemas']['BotRunHistoryPage'];
export interface CurrentRunState {
  readonly run: BotRunView | null;
  readonly loading: boolean;
  readonly failed: boolean;
}

export const EMPTY_CURRENT_RUN_STATE: CurrentRunState = Object.freeze({
  run: null,
  loading: false,
  failed: false,
});

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
/** The actions that only stop a bot or reduce its exposure (#2351). The
 * backend closes this set at `PanelQuiesceActionRequest.action_id`. */
export type PanelQuiesceActionId = components['schemas']['PanelQuiesceActionRequest']['action_id'];

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
/** Why the live snapshot is withheld; `PRODUCER_STALLED` is the typed stale state (#2353). */
export type LiveSnapshotUnavailableDetail = components['schemas']['LiveSnapshotUnavailableDetail'];

// ── §14 Operator-gated evidence ──────────────────────────────────────────────

export type EvidenceEntry = components['schemas']['EvidenceEntry'];
export type EvidencePage = components['schemas']['EvidencePage'];
