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

export interface DeployReadinessInput {
  daemonState: LinkState;
  daemonFreshness: DaemonFreshness;
  brokerState: LinkState;
  brokerDetail: string;
  accountTruth: Pick<AccountTruthResponse, 'final_verdict' | 'status_detail'> | null | undefined;
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

export function buildDeployReadinessFacts(input: DeployReadinessInput): DeployReadinessFact[] {
  return [
    engineReadinessFact(input.daemonState, input.daemonFreshness),
    brokerReadinessFact(input.brokerState, input.brokerDetail),
    accountReadinessFact(input.accountTruth, input.brokerAccountAvailable),
    fleetReadinessFact(input.fleetState, input.nothingDeployed),
  ];
}

export function buildNowChecks(input: NowChecksInput): DeployStatusCheck[] {
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
      state: input.fleetState,
      detail:
        input.fleetState === 'warn'
          ? 'Starts blocked'
          : input.fleetState === 'unknown'
            ? input.nothingDeployed
              ? 'Nothing deployed'
              : 'Checking'
            : 'Clear',
    },
  ];
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
): DeployReadinessFact {
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
): DeployReadinessFact {
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
