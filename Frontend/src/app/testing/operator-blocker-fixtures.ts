import type {
  BlockerSeverity,
  Disposition,
  OperatorBlocker,
  OperatorBlockerAnchor,
  OperatorBlockerAudience,
  OperatorConditionScope,
  OperatorHost,
  OperatorMove,
} from '../api/operator-blocker.types';

interface OperatorBlockerFixtureOptions {
  readonly id?: string;
  readonly scope?: OperatorConditionScope;
  readonly host?: OperatorHost;
  readonly anchor?: OperatorBlockerAnchor;
  readonly audience?: OperatorBlockerAudience;
  readonly severity?: BlockerSeverity;
  readonly disposition?: Disposition;
  readonly headline?: string;
  readonly detail?: string | null;
  readonly primaryMove?: OperatorMove | null;
  readonly secondaryMoves?: OperatorMove[];
  readonly appliesTo?: 'deploy' | 'run' | 'both';
}

const DEFAULT_MOVE: OperatorMove = {
  label: 'Connect the broker',
  action: { kind: 'navigate', route: '/broker', fragment: null },
  target: null,
};

function hasOwnOption<K extends keyof OperatorBlockerFixtureOptions>(
  options: OperatorBlockerFixtureOptions,
  key: K,
): boolean {
  return Object.prototype.hasOwnProperty.call(options, key);
}

export function operatorBlockerFixture(
  options: OperatorBlockerFixtureOptions = {},
): OperatorBlocker {
  const id = options.id ?? 'broker_disconnected';
  const severity = options.severity ?? 'blocking';
  return {
    condition: {
      id,
      severity,
      scope: options.scope ?? 'broker',
      evidence: {},
    },
    host: options.host ?? 'bot_cockpit',
    anchor: options.anchor ?? { kind: 'surface', subject_key: null },
    audience: options.audience ?? 'operator',
    disposition: options.disposition ?? 'fix_elsewhere',
    headline: options.headline ?? 'Broker disconnected',
    detail: hasOwnOption(options, 'detail')
      ? options.detail ?? null
      : 'Connect the IBKR session before deploying.',
    primary_move: hasOwnOption(options, 'primaryMove') ? options.primaryMove ?? null : DEFAULT_MOVE,
    secondary_moves: options.secondaryMoves ?? [],
    applies_to: options.appliesTo ?? 'both',
  };
}
