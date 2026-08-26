// TS mirror of PythonDataService/app/schemas/operator_blocker.py. Backend
// authors operator prose; the frontend renders headline/detail/labels verbatim.
export type Disposition = 'fix_here' | 'fix_elsewhere' | 'wait' | 'terminal';
export type OperatorHost =
  | 'bot_cockpit'
  | 'deploy_preflight'
  | 'fleet_roster'
  | 'account_monitor'
  | 'account_desk';
export type OperatorConditionScope = 'bot' | 'account' | 'broker' | 'fleet' | 'host' | 'strategy';
export const OPERATOR_BLOCKER_ANCHOR_KINDS = [
  'surface',
  'verdict',
  'lease',
  'clerk',
  'reconciliation',
  'holdings_row',
  'event',
  'cure_tools',
] as const;
export type OperatorBlockerAnchorKind = (typeof OPERATOR_BLOCKER_ANCHOR_KINDS)[number];
export type OperatorBlockerAudience = 'trader' | 'operator' | 'both';
export type AccountDeskLens = 'trader' | 'operator';

const SUBJECT_KEY_ANCHOR_KINDS: ReadonlySet<OperatorBlockerAnchorKind> = new Set([
  'holdings_row',
  'event',
]);

export interface OperatorBlockerAnchor {
  kind: OperatorBlockerAnchorKind;
  /** Opaque routing token; never render or normalize it as display copy. */
  subject_key: string | null;
}

/** Narrows an untrusted API value before reading named wire fields. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isOperatorBlockerAnchorKind(value: unknown): value is OperatorBlockerAnchorKind {
  return typeof value === 'string' && OPERATOR_BLOCKER_ANCHOR_KINDS.some((kind) => kind === value);
}

/**
 * Validates a wire anchor for a desk host. Future anchor kinds deliberately
 * collapse to the verdict card so a newer backend cannot make a blocker vanish
 * for an older client.
 */
export function accountDeskAnchorOrVerdictFallback(value: unknown): OperatorBlockerAnchor | null {
  if (!isRecord(value)) return null;

  const kind = value['kind'];
  const subjectKey = value['subject_key'];
  if (typeof kind !== 'string' || (typeof subjectKey !== 'string' && subjectKey !== null)) {
    return null;
  }
  if (!isOperatorBlockerAnchorKind(kind)) return { kind: 'verdict', subject_key: null };
  if (SUBJECT_KEY_ANCHOR_KINDS.has(kind)) {
    return typeof subjectKey === 'string' && subjectKey.length > 0
      ? { kind, subject_key: subjectKey }
      : null;
  }
  return subjectKey === null ? { kind, subject_key: null } : null;
}

export interface NavigateAction {
  kind: 'navigate';
  route: string;
  // Optional: the wire schema default (`= None`) makes the OpenAPI contract
  // mark this not-required.
  fragment?: string | null;
}

export interface ConfirmInFormAction {
  kind: 'confirm_in_form';
  anchor: string;
}

export interface OpenRunbookAction {
  kind: 'open_runbook';
  slug: string;
}

export interface RetireReplaceAction {
  kind: 'retire_replace';
}

export interface RemoveAction {
  kind: 'remove';
}

export type OperatorAction =
  | NavigateAction
  | ConfirmInFormAction
  | OpenRunbookAction
  | RetireReplaceAction
  | RemoveAction;

export interface OperatorMove {
  label: string;
  action: OperatorAction;
  // Optional below: the wire schema default (`= None` / `Field(default_factory=...)`)
  // makes the OpenAPI contract mark these not-required.
  target?: string | null;
  confirmation?: OperatorConfirmationCopy | null;
}

export interface OperatorConfirmationCopy {
  title: string;
  body: string;
  consequence: string;
  confirm_label: string;
  required_token?: string;
}

export type BlockerSeverity = 'blocking' | 'warning';

export interface OperatorCondition {
  id: string;
  severity: BlockerSeverity;
  scope: OperatorConditionScope;
  // Optional: the wire schema carries a `Field(default_factory=dict)` default,
  // so the generated OpenAPI contract does not mark it required.
  evidence?: Record<string, string | number | boolean | null>;
}

export interface OperatorBlocker {
  condition: OperatorCondition;
  host: OperatorHost;
  anchor: OperatorBlockerAnchor;
  /** Presentational routing only; never use it as an authorization decision. */
  audience: OperatorBlockerAudience;
  disposition: Disposition;
  headline: string;
  // Optional below: the wire schema default makes the OpenAPI contract mark
  // these not-required (matches OperatorMove/OperatorConfirmationCopy above).
  detail?: string | null;
  primary_move?: OperatorMove | null;
  secondary_moves?: OperatorMove[];
  applies_to: 'deploy' | 'run' | 'both';
}

/**
 * One canonical account-level operator decision, authored from one evidence
 * cut (issue #1664). `condition` is `null` exactly when the account is
 * healthy — in that case both host projections are also `null` and
 * `status_headline` / `status_detail` carry the backend-authored healthy
 * copy. When `condition` is set, `account_desk` and `fleet_roster` share
 * that one condition's identity/severity but carry host-relative
 * disposition, copy, and moves per ADR 0027. Consumers read only their own
 * host projection via `accountOperatorPostureBlocker` below and must never
 * fall back to the other host's projection or re-derive a verdict from raw
 * evidence.
 */
export interface AccountOperatorPosture {
  condition: OperatorCondition | null;
  account_desk: OperatorBlocker | null;
  fleet_roster: OperatorBlocker | null;
  status_headline: string;
  status_detail: string | null;
}

export type AccountOperatorPostureHost = 'account_desk' | 'fleet_roster';

/**
 * The `confirm_in_form` anchor the Alpaca operator lens recognizes to open
 * its in-place Clerk recovery panel. Mirrors
 * `ACCOUNT_DESK_RECOVERY_ANCHOR` in
 * `app/broker/alpaca/clerk/sqlite/account_operator_posture.py`.
 */
export const ACCOUNT_DESK_CLERK_RECOVERY_ANCHOR = 'account-desk-clerk-recovery';

/**
 * The `confirm_in_form` anchor the bot cockpit recognizes to run its own
 * reconciliation. Mirrors `BOT_COCKPIT_RECONCILE_ANCHOR` in
 * `app/services/broker_v2_panel/sqlite_panel_adapter.py`, which attaches
 * this move to every `fix_here` stale-evidence blocker.
 */
export const BOT_COCKPIT_RECONCILE_ANCHOR = 'bot-reconciliation-action';

/**
 * Backend-authored move list for one blocker, honoring the ADR 0027
 * disposition rules. `wait` never carries a move (the cure is elsewhere,
 * by design); every other disposition renders its primary move followed
 * by any secondary moves the backend attaches — the schema permits
 * `secondary_moves` on any non-`wait` disposition, not just `terminal`.
 */
export function movesForBlocker(blocker: OperatorBlocker): readonly OperatorMove[] {
  const secondaryMoves = blocker.secondary_moves ?? [];
  if (blocker.disposition === 'wait') return [];
  return blocker.primary_move ? [blocker.primary_move, ...secondaryMoves] : secondaryMoves;
}

/**
 * Selects the one host projection a surface may render. Returns `null`
 * (fail closed, never re-derive from raw evidence) when `posture` itself
 * is `null` — the only case this function guards. The backend's
 * `AccountOperatorPosture` validator (not this selector) is what makes a
 * partial projection — a non-null `condition` with one host's blocker
 * missing — impossible on the wire in the first place.
 */
export function accountOperatorPostureBlocker(
  posture: AccountOperatorPosture | null,
  host: AccountOperatorPostureHost,
): OperatorBlocker | null {
  if (posture === null) return null;
  return host === 'account_desk' ? posture.account_desk : posture.fleet_roster;
}

/** Returns the projections whose full backend-authored guidance belongs in a lens. */
export function operatorBlockersForAccountDeskLens(
  blockers: readonly OperatorBlocker[],
  lens: AccountDeskLens,
): readonly OperatorBlocker[] {
  return blockers.filter((blocker) => blocker.audience === lens || blocker.audience === 'both');
}

/**
 * Counts operator-only conditions for the trader's neutral attention summary.
 * Multiple host projections of one condition remain one item.
 */
export function operatorAttentionConditionCount(blockers: readonly OperatorBlocker[]): number {
  const traderVisibleConditionIds = new Set(
    blockers
      .filter((blocker) => blocker.audience === 'trader' || blocker.audience === 'both')
      .map((blocker) => blocker.condition.id),
  );
  return new Set(
    blockers
      .filter((blocker) => blocker.audience === 'operator' && !traderVisibleConditionIds.has(blocker.condition.id))
      .map((blocker) => blocker.condition.id),
  ).size;
}

export interface DeployPreflightResponse {
  ready: boolean;
  blockers: OperatorBlocker[];
}
