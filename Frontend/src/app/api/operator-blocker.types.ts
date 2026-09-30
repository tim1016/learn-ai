// TS mirror of PythonDataService/app/schemas/operator_blocker.py. Backend
// authors operator prose; the frontend renders headline/detail/labels verbatim.
export type Disposition = 'fix_here' | 'fix_elsewhere' | 'wait' | 'terminal';
export type OperatorHost =
  | 'bot_cockpit'
  | 'deploy_preflight'
  | 'account_monitor';
export type OperatorConditionScope = 'bot' | 'account' | 'broker' | 'fleet' | 'host' | 'strategy';
export const OPERATOR_BLOCKER_ANCHOR_KINDS = [
  'surface',
  'verdict',
  'lease',
  'reconciliation',
  'holdings_row',
  'event',
  'cure_tools',
] as const;
export type OperatorBlockerAnchorKind = (typeof OPERATOR_BLOCKER_ANCHOR_KINDS)[number];

export interface OperatorBlockerAnchor {
  kind: OperatorBlockerAnchorKind;
  /** Opaque routing token; never render or normalize it as display copy. */
  subject_key: string | null;
}

/** Narrows an untrusted API value before reading named wire fields. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
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

export interface RemoveAction {
  kind: 'remove';
}

export type OperatorAction =
  | NavigateAction
  | ConfirmInFormAction
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
 * The `confirm_in_form` anchor the bot cockpit recognizes to run its own
 * reconciliation. Mirrors `BOT_COCKPIT_RECONCILE_ANCHOR` in
 * `app/services/broker_v2_panel/sqlite_panel_adapter.py`, which attaches
 * this move to every `fix_here` stale-evidence blocker.
 */
export const BOT_COCKPIT_RECONCILE_ANCHOR = 'bot-reconciliation-action';

/**
 * The `confirm_in_form` anchor the bot cockpit recognizes to run Prepare
 * safe flatten — where an extended-hours flatten is priced from the live bid
 * and ask (#2007). Mirrors `BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR` in
 * `app/services/broker_v2_panel/sqlite_panel_adapter.py`, which attaches this
 * move to the unpriced Execute safe flatten outside the regular session.
 */
export const BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR = 'bot-safe-flatten-prepare';

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

export interface DeployPreflightResponse {
  ready: boolean;
  blockers: OperatorBlocker[];
}
