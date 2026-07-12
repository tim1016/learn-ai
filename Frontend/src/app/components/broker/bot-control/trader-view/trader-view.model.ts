import type {
  BotLifecycleDisplayStatus,
  LiveInstanceStatus,
  OperatorSurfaceCurrentRisk,
  ReconciliationState,
  TradingSessionPhase,
} from '../../../../api/live-instances.types';
import { fmtSignedCurrency } from '../../format';
import { formatPosition, resolveVerdictCardModel } from '../lib/verdict-card-model';
import type { PresentedAction } from '../lib/suggested-action-renderer';

export type TraderViewTone = 'attention' | 'healthy' | 'neutral' | 'waiting';

export interface TraderMetric {
  readonly label: string;
  readonly value: string;
  readonly tone: TraderViewTone;
}

export interface TraderTimelineItem {
  readonly icon: string;
  readonly title: string;
  readonly detail: string;
  readonly tone: TraderViewTone;
}

export interface TraderTrustRow {
  readonly icon: string;
  readonly label: string;
  readonly value: string;
  readonly detail: string;
  readonly tone: TraderViewTone;
}

export interface TraderHolding {
  readonly symbol: string;
  readonly quantity: number;
}

export interface TraderViewModel {
  readonly headline: string;
  readonly explanation: string;
  readonly tone: TraderViewTone;
  readonly marketTitle: string;
  readonly marketDetail: string;
  readonly marketPhase: TradingSessionPhase;
  readonly heroIcon: string;
  readonly metrics: readonly TraderMetric[];
  readonly holdings: readonly TraderHolding[];
  readonly timeline: readonly TraderTimelineItem[];
  readonly trustRows: readonly TraderTrustRow[];
  readonly primaryActionLabel: string | null;
  readonly terminalActions: ReturnType<typeof resolveVerdictCardModel>['terminalMoves'];
}

export const MARKET_PHASES: readonly {
  readonly id: Exclude<TradingSessionPhase, 'UNKNOWN'>;
  readonly label: string;
}[] = [
  { id: 'PRE', label: 'Pre-market' },
  { id: 'RTH', label: 'Market open' },
  { id: 'POST', label: 'After hours' },
  { id: 'OVERNIGHT', label: 'Overnight' },
  { id: 'CLOSED', label: 'Closed' },
];

const HEADLINE_BY_STATE: Record<BotLifecycleDisplayStatus, string> = {
  'Off duty': 'This bot is resting',
  Ready: 'This bot is ready',
  'On duty': 'This bot is on duty',
  'Clocking out': 'This bot is wrapping up',
  'Sick bay': 'This bot needs attention',
  'Off roster': 'This bot is off the roster',
  Retired: 'This deployment has ended',
};

const TONE_BY_STATE: Record<BotLifecycleDisplayStatus, TraderViewTone> = {
  'Off duty': 'neutral',
  Ready: 'healthy',
  'On duty': 'healthy',
  'Clocking out': 'waiting',
  'Sick bay': 'attention',
  'Off roster': 'neutral',
  Retired: 'neutral',
};

const ICON_BY_STATE: Record<BotLifecycleDisplayStatus, string> = {
  'Off duty': 'pi-pause',
  Ready: 'pi-check',
  'On duty': 'pi-bolt',
  'Clocking out': 'pi-clock',
  'Sick bay': 'pi-exclamation-triangle',
  'Off roster': 'pi-minus',
  Retired: 'pi-flag',
};

const MARKET_TITLE: Record<TradingSessionPhase, string> = {
  PRE: 'Pre-market',
  RTH: 'Market open',
  POST: 'After hours',
  OVERNIGHT: 'Overnight',
  CLOSED: 'Market closed',
  UNKNOWN: 'Market hours unavailable',
};

const NEXT_MARKET_EVENT: Record<TradingSessionPhase, string> = {
  PRE: 'Market opens',
  RTH: 'Regular trading ends',
  POST: 'After-hours trading ends',
  OVERNIGHT: 'Overnight trading ends',
  CLOSED: 'Market opens',
  UNKNOWN: 'Next session change',
};

function marketDetail(status: LiveInstanceStatus): string {
  const session = status.operator_surface.trading_session;
  if (session.next_transition_ms === null) return 'Next session time is not available';
  const time = new Intl.DateTimeFormat('en-US', {
    weekday: 'short',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: session.timezone,
    timeZoneName: 'short',
  }).format(session.next_transition_ms);
  return `${NEXT_MARKET_EVENT[session.phase]} ${time}`;
}

function positionCount(risk: OperatorSurfaceCurrentRisk): string {
  if (risk.posture === 'UNKNOWN') return 'Not proven';
  return String(Object.values(risk.owned_positions).filter((quantity) => quantity !== 0).length);
}

function pnlMetric(risk: OperatorSurfaceCurrentRisk): TraderMetric {
  const pnl = risk.unrealized_pnl;
  return {
    label: 'Unrealized P&L',
    value: pnl === null ? 'Not proven' : fmtSignedCurrency(pnl),
    tone: pnl === null ? 'neutral' : pnl < 0 ? 'attention' : pnl > 0 ? 'healthy' : 'neutral',
  };
}

function reconciliationLabel(state: ReconciliationState | null): string {
  switch (state) {
    case 'CLEAN': return 'Account checked';
    case 'ADOPTED': return 'Account activity recovered';
    case 'IN_PROGRESS': return 'Account check running';
    case 'STALE': return 'Account check is stale';
    case 'FAILED': return 'Account check failed';
    case 'NOT_AVAILABLE': return 'Not checked yet';
    default: return 'Account check unavailable';
  }
}

function brokerConnectionLabel(
  connection: LiveInstanceStatus['operator_surface']['broker']['connection'],
): string {
  switch (connection) {
    case 'CONNECTED': return 'Healthy';
    case 'DISCONNECTED': return 'Disconnected';
    case 'DEGRADED': return 'Limited';
    case 'UNKNOWN': return 'Not proven';
  }
}

function riskVerdictLabel(
  verdict: LiveInstanceStatus['operator_surface']['current_risk']['verdict'],
): string {
  switch (verdict) {
    case 'READY': return 'Clear';
    case 'ATTENTION': return 'Needs attention';
    case 'UNKNOWN': return 'Not proven';
  }
}

function sessionPermissionDetail(permitsActivity: boolean | null): string {
  if (permitsActivity === true) return 'The strategy is inside an allowed trading session.';
  if (permitsActivity === false) return 'The strategy is outside its allowed trading session.';
  return 'Trading permission is not proven for the current session.';
}

function runSignalTone(
  tone: LiveInstanceStatus['operator_surface']['run_signal']['tone'],
): TraderViewTone {
  if (tone === 'on') return 'healthy';
  if (tone === 'attention') return 'attention';
  if (tone === 'transition') return 'waiting';
  return 'neutral';
}

function reconciliationTone(state: ReconciliationState | null): TraderViewTone {
  if (state === 'CLEAN' || state === 'ADOPTED') return 'healthy';
  if (state === 'IN_PROGRESS') return 'waiting';
  if (state === 'FAILED' || state === 'STALE') return 'attention';
  return 'neutral';
}

function primaryActionLabel(
  status: LiveInstanceStatus,
  remediation: PresentedAction | null,
): string | null {
  const verb = resolveVerdictCardModel(status).verb;
  switch (verb.kind) {
    case 'lifecycle': return verb.action.label;
    case 'blocker_move': return verb.move.label;
    case 'remediation': return remediation?.label ?? null;
    case 'crash_recovery': return 'Record recovery evidence';
    case 'evidence': return 'View recovery details';
    case 'none': return null;
  }
}

export function resolveTraderViewModel(
  status: LiveInstanceStatus,
  remediation: PresentedAction | null,
): TraderViewModel {
  const surface = status.operator_surface;
  const lifecycle = status.daily_lifecycle;
  const verdict = resolveVerdictCardModel(status);
  const reconciliationState = surface.reconciliation?.state ?? null;
  const nextMarketEvent = marketDetail(status);
  const holdings = Object.entries(surface.current_risk.owned_positions)
    .filter(([, quantity]) => quantity !== 0)
    .map(([symbol, quantity]) => ({ symbol, quantity }));

  return {
    headline: HEADLINE_BY_STATE[lifecycle.display_status],
    explanation: verdict.reason ?? surface.trader_guidance.explanation,
    tone: TONE_BY_STATE[lifecycle.display_status],
    marketTitle: MARKET_TITLE[surface.trading_session.phase],
    marketDetail: nextMarketEvent,
    marketPhase: surface.trading_session.phase,
    heroIcon: ICON_BY_STATE[lifecycle.display_status],
    metrics: [
      pnlMetric(surface.current_risk),
      { label: 'Exposure', value: formatPosition(surface.current_risk), tone: 'neutral' },
      { label: 'Positions', value: positionCount(surface.current_risk), tone: 'neutral' },
      {
        label: 'Working orders',
        value: surface.current_risk.pending_order_count === null
          ? 'Not proven'
          : String(surface.current_risk.pending_order_count),
        tone: 'neutral',
      },
    ],
    holdings,
    timeline: [
      {
        icon: 'pi-clock',
        title: nextMarketEvent,
        detail: sessionPermissionDetail(surface.trading_session.permits_strategy_activity),
        tone: surface.trading_session.phase === 'UNKNOWN' ? 'attention' : 'waiting',
      },
      {
        icon: 'pi-shield',
        title: surface.submit_readiness.label,
        detail: surface.submit_readiness.explanation,
        tone: surface.submit_readiness.can_submit ? 'healthy' : 'attention',
      },
      {
        icon: 'pi-power-off',
        title: surface.run_signal.state_label,
        detail: surface.run_signal.detail,
        tone: runSignalTone(surface.run_signal.tone),
      },
    ],
    trustRows: [
      {
        icon: 'pi-link',
        label: 'Broker connection',
        value: brokerConnectionLabel(surface.broker.connection),
        detail: surface.broker.connection_condition.summary,
        tone: surface.broker.connection === 'CONNECTED' ? 'healthy' : 'attention',
      },
      {
        icon: 'pi-shield',
        label: 'Account safety',
        value: riskVerdictLabel(surface.current_risk.verdict),
        detail: surface.trader_guidance.risk_explanation,
        tone: surface.current_risk.verdict === 'READY' ? 'healthy' : 'attention',
      },
      {
        icon: 'pi-refresh',
        label: 'Reconciliation',
        value: reconciliationLabel(reconciliationState),
        detail: reconciliationState === null
          ? 'No current account comparison is available for this deployment.'
          : 'The latest broker and bot account comparison is shown here.',
        tone: reconciliationTone(reconciliationState),
      },
      {
        icon: 'pi-flag',
        label: 'Bot deployment',
        value: lifecycle.display_status,
        detail: lifecycle.reason ?? surface.run_signal.detail,
        tone: TONE_BY_STATE[lifecycle.display_status],
      },
    ],
    primaryActionLabel: primaryActionLabel(status, remediation),
    terminalActions: verdict.terminalMoves,
  };
}
