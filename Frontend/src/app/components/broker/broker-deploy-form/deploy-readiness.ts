import type {
  AccountConditionRow,
  AccountTriageResponse,
} from '../../../api/account-reconciliation.types';
import type { AccountTruthResponse } from '../../../api/broker-models';
import type { GateResult, HostProcessStartCapability } from '../../../api/live-instances.types';
import type { DesiredStateView } from '../../../api/live-runs-controls.types';
import type {
  DaemonFreshness,
  LinkState,
} from '../../../services/broker-connectivity.service';

export type DeployStatusState = LinkState | 'pending';
export type DeployReadinessState = LinkState;

export interface DeployStatusCheck {
  key: string;
  label: string;
  state: DeployStatusState;
  detail: string;
}

export interface DeployReadinessFact {
  key: 'engine' | 'broker' | 'account' | 'fleet';
  label: string;
  condition: string;
  detail: string;
  state: DeployReadinessState;
  link: string;
}

export interface AccountProofBlock {
  message: string;
  route: string;
  fragment: string;
  linkText: string;
}

export interface DeployBlocker {
  message: string;
  actionLink?: AccountProofBlock;
}

export interface DeployReadinessInput {
  daemonState: LinkState;
  daemonFreshness: DaemonFreshness;
  brokerState: LinkState;
  brokerDetail: string;
  accountTruth: Pick<AccountTruthResponse, 'final_verdict' | 'status_detail'> | null | undefined;
  accountTriage: Pick<AccountTriageResponse, 'conditions'> | null | undefined;
  brokerAccountAvailable: boolean;
  fleetState: LinkState;
  nothingDeployed: boolean;
}

export interface NowChecksInput {
  daemonState: LinkState;
  brokerState: LinkState;
  fieldsReady: boolean;
  fleetState: LinkState;
  nothingDeployed: boolean;
  accountTriage: Pick<AccountTriageResponse, 'conditions'> | null | undefined;
}

export interface DeployBlockerInput {
  daemonDown: boolean;
  effectiveStartNow: boolean;
  executionMode: 'read_only' | 'paper_orders' | 'live';
  allInCoexistenceBlock: string | null;
  fleetBlocksStarts: boolean;
  instanceAlreadyRunning: boolean;
  instanceId: string;
  brokerAccountAvailable: boolean;
  accountTruth: AccountTruthResponse | null | undefined;
  accountTriage: Pick<AccountTriageResponse, 'conditions'> | null | undefined;
  strategyKey: string;
  strategySelected: boolean;
  required: boolean;
  missingRequiredFields: string[];
  identityConflictSummary: string | null;
  exposureConflictSummary: string | null;
  actionPlanReadiness: ActionPlanDeployReadiness;
  customSizingError: string | null;
  stoppedStartLatchState: StoppedStartLatchState;
}

export const STOPPED_REQUIRES_RESUME = 'STOPPED_REQUIRES_RESUME';

export type StoppedStartLatchState = 'not_applicable' | 'checking' | 'unknown' | 'clear' | 'blocked';

export interface StoppedStartLatchInput {
  startNow: boolean;
  instanceId: string;
  instanceIdValid: boolean;
  statusLoading: boolean;
  statusUnavailable: boolean;
  desiredState: DesiredStateView | null | undefined;
  startCapability: Pick<
    HostProcessStartCapability,
    'disabled_reason_code' | 'gate_results'
  > | null | undefined;
}

export type ActionPlanDeployReasonCode =
  | 'ACTION_PLAN_EMPTY'
  | 'ACTION_PLAN_ENTRY_LEG_REQUIRED'
  | 'ACTION_PLAN_UNSUPPORTED'
  | 'ACTION_PLAN_CLOSE_LEG_REQUIRED';

export interface ActionPlanDeployReadiness {
  canDeploy: boolean;
  reasonCode: ActionPlanDeployReasonCode | null;
  message: string;
}

interface ActionPlanEnvelope {
  on_enter: unknown[];
  on_exit: unknown[];
}

interface ReadableActionPlanEntry {
  legId: string;
  instrumentKind: string;
  position: string;
}

const ACTION_PLAN_REQUIRED_STRATEGIES = new Set(['deployment_validation']);
const ACTION_PLAN_READY: ActionPlanDeployReadiness = {
  canDeploy: true,
  reasonCode: null,
  message: 'Action plan is ready for deployment.',
};

export function buildDeployReadinessFacts(input: DeployReadinessInput): DeployReadinessFact[] {
  const freeze = activeAccountFreezeCondition(input.accountTriage);
  return [
    engineReadinessFact(input.daemonState, input.daemonFreshness),
    brokerReadinessFact(input.brokerState, input.brokerDetail),
    accountReadinessFact(input.accountTruth, input.brokerAccountAvailable, freeze),
    fleetReadinessFact(input.fleetState, input.nothingDeployed, freeze),
  ];
}

export function buildNowChecks(input: NowChecksInput): DeployStatusCheck[] {
  const freeze = activeAccountFreezeCondition(input.accountTriage);
  const fleetCheck = fleetNowCheck(input, freeze);
  return [
    {
      key: 'engine',
      label: 'Engine up',
      state: input.daemonState,
      detail:
        input.daemonState === 'ok'
          ? 'Ready'
          : input.daemonState === 'unknown'
            ? 'Checking'
            : 'Start it, then recheck',
    },
    {
      key: 'broker',
      label: 'Broker',
      state: input.brokerState,
      detail:
        input.brokerState === 'ok'
          ? 'Connected'
          : input.brokerState === 'unknown'
            ? 'Checking'
            : 'Disconnected',
    },
    {
      key: 'fields',
      label: 'Fields',
      state: input.fieldsReady ? 'ok' : 'warn',
      detail: input.fieldsReady ? 'Complete' : 'Required fields missing',
    },
    {
      key: 'fleet',
      label: 'Fleet clear',
      state: fleetCheck.state,
      detail: fleetCheck.detail,
    },
  ];
}

function fleetNowCheck(
  input: NowChecksInput,
  freeze: AccountConditionRow | null,
): Pick<DeployStatusCheck, 'state' | 'detail'> {
  if (freeze !== null) return { state: 'warn', detail: 'Account frozen' };
  if (input.fleetState === 'warn') return { state: input.fleetState, detail: 'Starts blocked' };
  if (input.fleetState === 'unknown') {
    return {
      state: input.fleetState,
      detail: input.nothingDeployed ? 'Nothing deployed' : 'Checking',
    };
  }
  return { state: input.fleetState, detail: 'Clear' };
}

export function deployBlocker(input: DeployBlockerInput): DeployBlocker | null {
  if (input.daemonDown) {
    return { message: 'Live engine unavailable. Start it on this machine, then recheck.' };
  }
  if (input.effectiveStartNow && input.executionMode === 'live') {
    return {
      message: 'Live execution is not available from Deploy yet. Pick read-only or paper orders.',
    };
  }
  if (input.allInCoexistenceBlock !== null) return { message: input.allInCoexistenceBlock };
  const freeze = activeAccountFreezeCondition(input.accountTriage);
  if (input.effectiveStartNow && freeze !== null) {
    return {
      message: 'Account freeze active. Resolve the account sick-bay condition before starting.',
      actionLink: {
        message: 'Account freeze active.',
        route: '/broker/account-monitor',
        fragment: 'account-reconciliation-action',
        linkText: 'Open account monitor',
      },
    };
  }
  if (input.effectiveStartNow && input.fleetBlocksStarts) {
    return {
      message: 'Fleet state blocks new starts. Turn off "Start trading immediately" to deploy only, or clear the account state.',
    };
  }
  if (input.effectiveStartNow && input.instanceAlreadyRunning) {
    return {
      message: `"${input.instanceId}" is already running. Stop it first, or turn off "Start trading immediately" to deploy without starting.`,
    };
  }
  if (!input.brokerAccountAvailable) {
    return { message: 'Connected broker account unavailable. Connect the broker session before deploying.' };
  }
  if (input.effectiveStartNow) {
    if (!input.accountTruth) {
      return {
        message: 'Account proof is still loading. Wait for account readiness, or turn off "Start trading immediately" to deploy only.',
      };
    }
    const accountProofBlock = buildAccountProofBlock(input.accountTruth);
    if (accountProofBlock) {
      return {
        message: accountProofBlock.message,
        actionLink: accountProofBlock,
      };
    }
  }
  if (input.strategyKey.trim() !== '' && !input.strategySelected) {
    return {
      message: 'Strategy must be validated before deployment. Open Strategy Validation to promote it.',
    };
  }
  if (!input.required) {
    return { message: 'Missing: ' + input.missingRequiredFields.join(', ') + '.' };
  }
  if (input.identityConflictSummary !== null) return { message: input.identityConflictSummary };
  if (input.exposureConflictSummary !== null) return { message: input.exposureConflictSummary };
  if (!input.actionPlanReadiness.canDeploy) return { message: input.actionPlanReadiness.message };
  if (input.customSizingError !== null) return { message: input.customSizingError };
  if (input.effectiveStartNow && input.stoppedStartLatchState === 'checking') {
    return {
      message: 'Checking durable desired state before starting. Wait for the latch check, or turn off "Start trading immediately" to deploy only.',
    };
  }
  if (input.effectiveStartNow && input.stoppedStartLatchState === 'unknown') {
    return {
      message: 'Could not verify durable desired state before starting. Retry the status check, or turn off "Start trading immediately" to deploy only.',
    };
  }
  return null;
}

export function actionPlanDeployReadiness(
  strategyKey: string,
  actionPlan: unknown,
): ActionPlanDeployReadiness {
  if (!ACTION_PLAN_REQUIRED_STRATEGIES.has(strategyKey.trim())) {
    return ACTION_PLAN_READY;
  }
  if (!hasActionPlanEnvelope(actionPlan)) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_EMPTY',
      message: 'Deployment Validation requires an action plan with one long stock entry leg and a matching close leg before deployment.',
    };
  }
  const hasEntries = actionPlan.on_enter.length > 0;
  const hasExits = actionPlan.on_exit.length > 0;
  if (!hasEntries && !hasExits) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_EMPTY',
      message: 'Deployment Validation requires an action plan; ON ENTER and ON EXIT are both empty.',
    };
  }
  if (!hasEntries) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_ENTRY_LEG_REQUIRED',
      message: 'Deployment Validation requires at least one ON ENTER entry leg.',
    };
  }
  const firstEntry = readableFirstEntry(actionPlan);
  if (firstEntry === null) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_UNSUPPORTED',
      message: 'Deployment Validation cannot consume this action-plan shape. Use one long stock entry leg with a close-leg exit.',
    };
  }
  if (
    actionPlan.on_enter.length !== 1 ||
    firstEntry.instrumentKind !== 'stock' ||
    firstEntry.position !== 'long'
  ) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_UNSUPPORTED',
      message: 'Deployment Validation currently supports exactly one long stock entry leg. Option, short, and multi-leg plans are not deployable on this runtime path yet.',
    };
  }
  if (!actionPlan.on_exit.some((exit) => closeLegReferences(exit, firstEntry.legId))) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_CLOSE_LEG_REQUIRED',
      message: `Deployment Validation requires an ON EXIT close leg for the entry leg '${firstEntry.legId}'.`,
    };
  }
  return ACTION_PLAN_READY;
}

function hasActionPlanEnvelope(value: unknown): value is ActionPlanEnvelope {
  if (value === null || typeof value !== 'object') return false;
  return 'on_enter' in value && 'on_exit' in value && Array.isArray(value.on_enter) && Array.isArray(value.on_exit);
}

function readableFirstEntry(actionPlan: ActionPlanEnvelope): ReadableActionPlanEntry | null {
  const entry = actionPlan.on_enter[0];
  if (entry === null || typeof entry !== 'object') return null;
  if (!('leg_id' in entry) || !('instrument' in entry) || !('position' in entry)) {
    return null;
  }
  const instrument = entry.instrument;
  if (instrument === null || typeof instrument !== 'object' || !('kind' in instrument)) {
    return null;
  }
  if (typeof entry.leg_id !== 'string' || typeof instrument.kind !== 'string') {
    return null;
  }
  return typeof entry.position === 'string'
    ? { legId: entry.leg_id, instrumentKind: instrument.kind, position: entry.position }
    : null;
}

function closeLegReferences(exit: unknown, entryLegId: string): boolean {
  return (
    exit !== null &&
    typeof exit === 'object' &&
    'entry_leg_id' in exit &&
    exit.entry_leg_id === entryLegId
  );
}

function buildAccountProofBlock(truth: AccountTruthResponse): AccountProofBlock | null {
  if (truth.final_verdict !== 'not_proven') return null;
  const missingEvidence = accountProofMissingEvidence(truth);
  const evidenceDetail = missingEvidence.length > 0
    ? ` Missing evidence: ${missingEvidence.join(' ')}`
    : '';
  return {
    message: (
      'Account proof is not proven. Reconcile account before starting, or turn off ' +
      `"Start trading immediately" to deploy only.${evidenceDetail}`
    ),
    route: '/broker/account-monitor',
    fragment: 'account-reconciliation-action',
    linkText: 'Run account reconcile',
  };
}

function accountProofMissingEvidence(truth: AccountTruthResponse): string[] {
  const criticalSources = (truth.source_freshness ?? [])
    .filter((source) => source.severity === 'critical' && source.status !== 'fresh')
    .map((source) => source.message);
  const evidenceGaps = (truth.evidence_gaps ?? [])
    .filter((gap) => gap.severity === 'critical' || gap.severity === 'warning')
    .map((gap) => gap.message);
  const invariantFailures = (truth.invariants ?? [])
    .filter((invariant) => invariant.status === 'fail' || invariant.severity === 'critical')
    .map((invariant) => invariant.headline || invariant.narrative || invariant.label);
  const blockers = (truth.blockers ?? []).map((blocker) => blocker.title || blocker.message);
  return [...new Set([...criticalSources, ...evidenceGaps, ...invariantFailures, ...blockers])]
    .filter((label) => label.trim() !== '')
    .slice(0, 3);
}

export function buildDeployChecks(errorStatus: number | null | undefined): DeployStatusCheck[] {
  return [
    {
      key: 'tree',
      label: 'Working tree clean',
      state: errorStatus === 409 ? 'down' : 'pending',
      detail: errorStatus === 409
        ? 'Commit or stash the listed files'
        : 'Checked when you deploy',
    },
    {
      key: 'spec',
      label: 'Spec matches strategy',
      state: errorStatus === 400 ? 'down' : 'pending',
      detail: errorStatus === 400
        ? 'Pick the matching spec'
        : 'Checked when you deploy',
    },
  ];
}

export function stoppedStartLatchState(input: StoppedStartLatchInput): StoppedStartLatchState {
  if (!input.startNow || input.instanceId.trim() === '' || !input.instanceIdValid) {
    return 'not_applicable';
  }
  if (input.statusLoading) {
    return 'checking';
  }
  if (input.statusUnavailable) {
    return 'unknown';
  }
  if (
    input.desiredState?.state === 'STOPPED' ||
    input.startCapability?.disabled_reason_code === STOPPED_REQUIRES_RESUME ||
    gatesRequireResume(input.startCapability?.gate_results ?? [])
  ) {
    return 'blocked';
  }
  return 'clear';
}

function gatesRequireResume(gates: GateResult[]): boolean {
  return gates.some((gate) => gate.operator_next_step === STOPPED_REQUIRES_RESUME);
}

function engineReadinessFact(
  state: LinkState,
  freshness: DaemonFreshness,
): DeployReadinessFact {
  if (state === 'down') {
    return {
      key: 'engine',
      label: 'Engine',
      condition: 'Unreachable',
      detail: 'Start the local daemon, then recheck.',
      state: 'down',
      link: '/engine',
    };
  }
  if (freshness.state === 'stale') {
    return {
      key: 'engine',
      label: 'Engine',
      condition: 'Stale code',
      detail: 'Restart the daemon to apply the current repo.',
      state: 'warn',
      link: '/engine',
    };
  }
  if (state === 'ok') {
    return {
      key: 'engine',
      label: 'Engine',
      condition: 'Healthy',
      detail: freshness.sha ? `Running ${freshness.sha}` : 'Daemon reachable.',
      state: 'ok',
      link: '/engine',
    };
  }
  return {
    key: 'engine',
    label: 'Engine',
    condition: 'Checking',
    detail: 'Waiting for daemon health.',
    state: 'unknown',
    link: '/engine',
  };
}

function brokerReadinessFact(state: LinkState, detail: string): DeployReadinessFact {
  const condition =
    state === 'ok'
      ? 'Linked'
      : state === 'down'
        ? 'Hard down'
        : state === 'warn'
          ? 'Reconnecting'
          : 'Checking';
  return {
    key: 'broker',
    label: 'Broker',
    condition,
    detail,
    state,
    link: '/broker/session-mirror',
  };
}

function accountReadinessFact(
  truth: Pick<AccountTruthResponse, 'final_verdict' | 'status_detail'> | null | undefined,
  brokerAccountAvailable: boolean,
  freeze: AccountConditionRow | null,
): DeployReadinessFact {
  if (freeze !== null) {
    return {
      key: 'account',
      label: 'Account',
      condition: 'Frozen',
      detail: freeze.title,
      state: 'down',
      link: '/broker/account-monitor',
    };
  }
  if (!brokerAccountAvailable) {
    return {
      key: 'account',
      label: 'Account',
      condition: 'Not proven',
      detail: 'Broker account identity is unavailable.',
      state: 'down',
      link: '/broker/account-monitor',
    };
  }
  if (truth?.final_verdict === 'not_proven') {
    return {
      key: 'account',
      label: 'Account',
      condition: 'Not proven',
      detail: truth.status_detail,
      state: 'warn',
      link: '/broker/account-monitor',
    };
  }
  if (truth?.final_verdict === 'clean') {
    return {
      key: 'account',
      label: 'Account',
      condition: 'Clean',
      detail: truth.status_detail,
      state: 'ok',
      link: '/broker/account-monitor',
    };
  }
  return {
    key: 'account',
    label: 'Account',
    condition: 'Checking',
    detail: 'Waiting for account-truth proof.',
    state: 'unknown',
    link: '/broker/account-monitor',
  };
}

function fleetReadinessFact(
  state: LinkState,
  nothingDeployed: boolean,
  freeze: AccountConditionRow | null,
): DeployReadinessFact {
  if (freeze !== null) {
    return {
      key: 'fleet',
      label: 'Fleet',
      condition: 'Frozen',
      detail: 'New starts are blocked by the account sick bay.',
      state: 'warn',
      link: '/broker/account-monitor',
    };
  }
  if (nothingDeployed) {
    return {
      key: 'fleet',
      label: 'Fleet',
      condition: 'Empty',
      detail: 'No deployed bots on this account.',
      state: 'unknown',
      link: '/broker/bots',
    };
  }
  if (state === 'warn') {
    return {
      key: 'fleet',
      label: 'Fleet',
      condition: 'Contaminated',
      detail: 'New starts are blocked until account state clears.',
      state: 'warn',
      link: '/broker/reconciliation',
    };
  }
  if (state === 'ok') {
    return {
      key: 'fleet',
      label: 'Fleet',
      condition: 'Clear',
      detail: 'No fleet policy blocks.',
      state: 'ok',
      link: '/broker/bots',
    };
  }
  return {
    key: 'fleet',
    label: 'Fleet',
    condition: 'Checking',
    detail: 'Waiting for fleet policy.',
    state: 'unknown',
    link: '/broker/bots',
  };
}

export function activeAccountFreezeCondition(
  triage: Pick<AccountTriageResponse, 'conditions'> | null | undefined,
): AccountConditionRow | null {
  return triage?.conditions.find((condition) =>
    condition.scope === 'account' &&
    (condition.condition_type === 'exposure_freeze' || condition.condition_type === 'account_freeze')
  ) ?? null;
}
