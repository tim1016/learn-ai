import type {
  ActionCapability,
  HostProcessStartCapability,
  LiveInstanceStatus,
} from '../../../../../api/live-instances.types';
import type {
  BotEventRow,
  BotEventType,
  TerminalErrorCode,
} from '../../../../../api/live-runs.types';
import { disabledReasonCopy } from '../../lib/disabled-reason-copy';

export const BOT_EVENT_TYPES: readonly BotEventType[] = [
  'evaluation_idle',
  'signal_fired',
  'order_submitted',
  'order_filled',
  'order_cancelled',
  'order_rejected',
  'blocked',
  'halted',
  'launch_failed',
];

export const TERMINAL_ERROR_CODES: readonly TerminalErrorCode[] = [
  'order_rejected',
  'submit_uncertain',
  'halted',
  'launch_failed',
  'unmapped_diagnostic',
];

export type BotEventStreamCommand =
  | 'start_process'
  | 'resume'
  | 'pause'
  | 'flatten_and_pause'
  | 'stop'
  | 'mark_poisoned'
  | 'fresh_run';

export interface BotEventStreamAction {
  readonly command: BotEventStreamCommand;
  readonly label: string;
  readonly enabled: boolean;
  readonly disabledReason: string | null;
}

const EVENT_ACTIONS: Record<BotEventType, BotEventStreamCommand | null> = {
  evaluation_idle: null,
  signal_fired: null,
  order_submitted: null,
  order_filled: null,
  order_cancelled: null,
  order_rejected: 'mark_poisoned',
  blocked: null,
  halted: 'fresh_run',
  launch_failed: 'start_process',
};

const TERMINAL_ACTIONS: Record<TerminalErrorCode, BotEventStreamCommand | null> = {
  order_rejected: 'mark_poisoned',
  submit_uncertain: 'flatten_and_pause',
  halted: 'fresh_run',
  launch_failed: 'start_process',
  unmapped_diagnostic: null,
};

const ACTION_LABELS: Record<BotEventStreamCommand, string> = {
  start_process: 'Start bot process',
  resume: 'Resume',
  pause: 'End day now',
  flatten_and_pause: 'Flatten & pause',
  stop: 'Stop instance',
  mark_poisoned: 'Mark poisoned',
  fresh_run: 'Fresh run',
};

export function actionCommandForRow(row: BotEventRow): BotEventStreamCommand | null {
  const terminalCode = row.terminal_error?.code ?? null;
  if (terminalCode !== null) return TERMINAL_ACTIONS[terminalCode];
  return EVENT_ACTIONS[row.event_type];
}

export function actionForRow(
  row: BotEventRow,
  status: LiveInstanceStatus,
  locallyDisabled: boolean,
): BotEventStreamAction | null {
  const command = actionCommandForRow(row);
  if (command === null) return null;
  if (locallyDisabled) {
    return {
      command,
      label: ACTION_LABELS[command],
      enabled: false,
      disabledReason: disabledReasonCopy('LOCAL_REQUEST_IN_FLIGHT'),
    };
  }
  if (command === 'fresh_run') {
    return { command, label: ACTION_LABELS[command], enabled: true, disabledReason: null };
  }
  if (command === 'start_process') {
    return actionFromHostStart(command, status.operator_surface.host_process.start_capability);
  }
  return actionFromCapability(command, status.operator_surface.actions[command]);
}

function actionFromCapability(
  command: Exclude<BotEventStreamCommand, 'fresh_run' | 'start_process'>,
  capability: ActionCapability,
): BotEventStreamAction {
  return {
    command,
    label: ACTION_LABELS[command],
    enabled: capability.enabled,
    disabledReason: capability.enabled
      ? null
      : disabledReasonCopy(capability.disabled_reason_code ?? capability.disabled_reasons[0]),
  };
}

function actionFromHostStart(
  command: 'start_process',
  capability: HostProcessStartCapability,
): BotEventStreamAction {
  return {
    command,
    label: ACTION_LABELS[command],
    enabled: capability.enabled,
    disabledReason: capability.enabled
      ? null
      : disabledReasonCopy(capability.disabled_reason_code),
  };
}
