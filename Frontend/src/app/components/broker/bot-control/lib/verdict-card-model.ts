// Verdict Card model — the single source of truth for the trader-first
// bot control page (docs/superpowers/specs/2026-07-08-bot-control-verdict-card-design.md).
//
// Pure resolver: given a backend-authored `LiveInstanceStatus`, it decides the
// state word, tone, layout, identity line, the one primary verb, the ambient
// actions, and the vitals row. It derives nothing the backend already
// authored — the state word is `daily_lifecycle.display_status` verbatim, the
// verb is `daily_lifecycle.primary_action` when present (else the trader
// remediation), and the numbers are read straight from `operator_surface`.
//
// Verb resolution lives here so the presentation component and the container's
// dispatch routing agree on which verb the state shows (spec: "one big verb per
// state", no disabled-button graveyard).

import type {
  BotLifecycleAction,
  BotLifecycleDisplayStatus,
  BrokerSafetyVerdict,
  LiveInstanceStatus,
  OperatorSurfaceCurrentRisk,
  RiskPosture,
  TradingSessionPhase,
} from '../../../../api/live-instances.types';
import type { OperatorBlocker, OperatorMove } from '../../../../api/operator-blocker.types';
import { fmtSignedCurrency } from '../../format';
import { runbookOpensInstancePage } from './operator-runbook-routes';

export type VerdictTone = 'danger' | 'positive' | 'neutral' | 'muted' | 'warning';
export type VerdictLayout = 'full' | 'strip';
export type VerdictMode = 'Paper' | 'Live' | 'Unknown';

export interface VerdictIdentity {
  readonly instanceId: string;
  readonly symbol: string | null;
  readonly mode: VerdictMode;
}

/** Which source the state's single verb comes from. The remediation branch
 *  carries no label/handler — those come from the container-computed
 *  `RenderedAction` so verb copy is never duplicated here. `crash_recovery` is
 *  the start-gate safety verb: when the start gate reports
 *  `CRASH_RECOVERY_REQUIRED` the operator must record recovery evidence before
 *  the bot can run again — a first-class verb so the capability is never
 *  silently dropped when the old notice banners are removed. `evidence` is a
 *  self-targeting `open_runbook` remediation: its runbook route is this bot's
 *  own page, so navigating is a no-op — the verb opens the why-drawer instead. */
export type VerdictVerbSource =
  | { readonly kind: 'lifecycle'; readonly action: BotLifecycleAction }
  | { readonly kind: 'remediation' }
  | { readonly kind: 'crash_recovery' }
  | { readonly kind: 'evidence' }
  | { readonly kind: 'none' };

/** UI-authored label for the synthetic crash-recovery verb (not backend prose —
 *  it opens the flat-account attestation dialog). */
export const CRASH_RECOVERY_VERB_LABEL = 'Record recovery evidence';

/** UI-authored label for the evidence verb (opens the why-drawer, which is the
 *  in-design home for this-bot recovery guidance). */
export const EVIDENCE_VERB_LABEL = 'View recovery details';

export type VitalTone = 'positive' | 'negative' | 'neutral';

export interface VerdictVital {
  readonly label: string;
  readonly value: string;
  readonly tone: VitalTone;
}

export interface VerdictCardModel {
  readonly state: BotLifecycleDisplayStatus;
  readonly stateLabel: string;
  readonly tone: VerdictTone;
  readonly layout: VerdictLayout;
  readonly identity: VerdictIdentity;
  /** Backend-authored one-line reason; rendered as prose, never piped. */
  readonly reason: string | null;
  readonly verb: VerdictVerbSource;
  readonly terminalBlocker: OperatorBlocker | null;
  readonly terminalMoves: readonly OperatorMove[];
  readonly ambientActions: readonly BotLifecycleAction[];
  /** Terminal cards expose only terminal moves; no lifecycle/settings overflow. */
  readonly showOverflow: boolean;
  /** Terminal cards suppress lifecycle-condition cures; terminal moves are the only exits. */
  readonly showConditionCure: boolean;
  readonly vitals: readonly VerdictVital[];
  /** Clocking out: show the live clean-exit checklist, never a verb. */
  readonly showChecklist: boolean;
  /** Retired: the card is a read-only record. */
  readonly readOnly: boolean;
  /** On duty: the price chart + trade evidence own the body. */
  readonly showChart: boolean;
  /** The evaluator flagged the projection as possibly stale. */
  readonly driftDetected: boolean;
}

const TONE_BY_STATE: Record<BotLifecycleDisplayStatus, VerdictTone> = {
  'Sick bay': 'danger',
  'On duty': 'positive',
  Ready: 'positive',
  'Off duty': 'neutral',
  'Off roster': 'muted',
  'Clocking out': 'warning',
  Retired: 'muted',
};

const SESSION_PHASE_LABEL: Record<TradingSessionPhase, string> = {
  PRE: 'Pre-market',
  RTH: 'Open',
  POST: 'After hours',
  CLOSED: 'Closed',
  UNKNOWN: 'Unknown',
};

function resolveMode(verdict: BrokerSafetyVerdict): VerdictMode {
  if (verdict === 'PAPER_ONLY') return 'Paper';
  if (verdict === 'UNSAFE') return 'Live';
  return 'Unknown';
}

/** "Flat" / "Long 40 SPY" / "Mixed" — a one-line position summary from the
 *  namespace-attributed risk slice. Never fabricates a size the broker did
 *  not report. */
export function formatPosition(risk: OperatorSurfaceCurrentRisk): string {
  const posture: RiskPosture = risk.posture;
  if (posture === 'FLAT') return 'Flat';
  if (posture === 'UNKNOWN') return 'Not proven';
  if (posture === 'MIXED') return 'Mixed';
  const entries = Object.entries(risk.owned_positions).filter(([, qty]) => qty !== 0);
  const word = posture === 'LONG' ? 'Long' : 'Short';
  if (entries.length === 1) {
    const [symbol, qty] = entries[0];
    return `${word} ${Math.abs(qty)} ${symbol}`;
  }
  return word;
}

function pnlVital(risk: OperatorSurfaceCurrentRisk): VerdictVital {
  const pnl = risk.unrealized_pnl;
  const tone: VitalTone = pnl === null ? 'neutral' : pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'neutral';
  return {
    label: 'Unrealized P&L',
    value: pnl === null ? 'Not proven' : fmtSignedCurrency(pnl),
    tone,
  };
}

function positionVital(risk: OperatorSurfaceCurrentRisk): VerdictVital {
  return { label: 'Position', value: formatPosition(risk), tone: 'neutral' };
}

function ordersVital(status: LiveInstanceStatus): VerdictVital {
  const cap = status.operator_surface.daily_order_cap;
  const used = cap.used ?? null;
  const limit = cap.limit ?? null;
  const value =
    limit === null ? 'No cap set' : used === null ? `— / ${limit}` : `${used} / ${limit}`;
  return { label: 'Orders today', value, tone: 'neutral' };
}

function sessionVital(status: LiveInstanceStatus): VerdictVital {
  const phase = status.operator_surface.trading_session.phase;
  return { label: 'Session', value: SESSION_PHASE_LABEL[phase], tone: 'neutral' };
}

function primaryBlocker(status: LiveInstanceStatus): OperatorBlocker | null {
  return status.operator_surface.blockers[0] ?? null;
}

function terminalMoves(blocker: OperatorBlocker | null): OperatorMove[] {
  if (!blocker || blocker.disposition !== 'terminal') return [];
  return blocker.primary_move
    ? [blocker.primary_move, ...blocker.secondary_moves]
    : [...blocker.secondary_moves];
}

function resolveVitals(state: BotLifecycleDisplayStatus, status: LiveInstanceStatus): VerdictVital[] {
  const risk = status.operator_surface.current_risk;
  switch (state) {
    case 'On duty':
      return [positionVital(risk), pnlVital(risk), ordersVital(status)];
    case 'Sick bay':
      return [positionVital(risk), pnlVital(risk), sessionVital(status)];
    default:
      return [];
  }
}

function resolveVerb(state: BotLifecycleDisplayStatus, status: LiveInstanceStatus): VerdictVerbSource {
  const blocker = primaryBlocker(status);
  if (blocker?.disposition === 'terminal') return { kind: 'none' };
  // Clocking out is the one state that never shows a verb — it is already doing
  // what the operator asked.
  if (state === 'Clocking out') return { kind: 'none' };
  const lifecycle = status.daily_lifecycle;
  // Retired is a read-only record: its only allowed verb is a lifecycle action
  // (Create replacement). Remediations never become a retired bot's primary verb.
  if (state === 'Retired') {
    return lifecycle.primary_action
      ? { kind: 'lifecycle', action: lifecycle.primary_action }
      : { kind: 'none' };
  }
  // A crash-retired start gate takes precedence: nothing else can run until the
  // operator attests the account is flat. Only meaningful when the bot is off.
  if (
    state !== 'On duty' &&
    status.operator_surface.host_process.start_capability.disabled_reason_code ===
      'CRASH_RECOVERY_REQUIRED'
  ) {
    return { kind: 'crash_recovery' };
  }
  if (lifecycle.primary_action) return { kind: 'lifecycle', action: lifecycle.primary_action };
  const remediation = status.operator_surface.trader_guidance.primary_remediation;
  if (remediation.kind === 'open_runbook' && runbookOpensInstancePage(remediation.slug)) {
    // The runbook route is this bot's own page — navigating is a no-op. Open the
    // why-drawer, which holds this bot's recovery evidence and blockers.
    return { kind: 'evidence' };
  }
  if (remediation.kind !== 'none') return { kind: 'remediation' };
  return { kind: 'none' };
}

export function resolveVerdictCardModel(status: LiveInstanceStatus): VerdictCardModel {
  const lifecycle = status.daily_lifecycle;
  const state = lifecycle.display_status;
  const blocker = primaryBlocker(status);
  const terminalBlocker = blocker?.disposition === 'terminal' ? blocker : null;
  return {
    state,
    stateLabel: terminalBlocker?.headline ?? state,
    tone: terminalBlocker ? 'danger' : TONE_BY_STATE[state],
    layout: state === 'On duty' ? 'strip' : 'full',
    identity: {
      instanceId: status.strategy_instance_id,
      symbol: status.symbol,
      mode: resolveMode(status.operator_surface.broker.safety_verdict),
    },
    reason: terminalBlocker?.detail ?? lifecycle.reason,
    verb: resolveVerb(state, status),
    terminalBlocker,
    terminalMoves: terminalMoves(terminalBlocker),
    ambientActions: terminalBlocker ? [] : lifecycle.ambient_actions,
    showOverflow: terminalBlocker === null,
    showConditionCure: terminalBlocker === null,
    vitals: resolveVitals(state, status),
    showChecklist: state === 'Clocking out',
    readOnly: state === 'Retired',
    showChart: state === 'On duty',
    driftDetected: lifecycle.drift_detected,
  };
}
