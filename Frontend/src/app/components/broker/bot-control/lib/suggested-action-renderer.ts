import type {
  GateSuggestedAction,
  InvokeCapabilityAction,
  InvokeEndpointAction,
  FocusAction,
  TraderPrimaryRemediation,
} from '../../../../api/live-instances.types';

export interface PresentedAction {
  readonly label: string;
  readonly variant: 'primary' | 'link';
}

const CAPABILITY_LABELS: Record<InvokeCapabilityAction['capability'], string> = {
  resume: 'Resume',
  pause: 'End day now',
};

const FOCUS_LABELS: Record<FocusAction['action'], string> = {
  flatten_and_pause: 'Open recovery action →',
  stop: 'Stop instance →',
  mark_poisoned: 'Mark poisoned →',
};

const ENDPOINT_LABELS: Record<InvokeEndpointAction['endpoint'], string> = {
  reconcile_instance: 'Reconcile now',
};

/** Sole dispatch-free label/variant presenter for backend-authored remediation. */
export function presentSuggestedAction(
  action: GateSuggestedAction | TraderPrimaryRemediation | null,
): PresentedAction | null {
  if (action === null || action.kind === 'none') return null;
  switch (action.kind) {
    case 'invoke_capability':
      return { label: CAPABILITY_LABELS[action.capability], variant: 'primary' };
    case 'focus_action':
      return { label: FOCUS_LABELS[action.action], variant: 'link' };
    case 'redeploy':
      return { label: 'Redeploy →', variant: 'link' };
    case 'open_runbook':
      return { label: 'Open runbook →', variant: 'link' };
    case 'invoke_endpoint':
      return { label: ENDPOINT_LABELS[action.endpoint], variant: 'primary' };
    default:
      return null;
  }
}

export function presentTraderRemediation(
  action: TraderPrimaryRemediation | null,
): PresentedAction | null {
  return presentSuggestedAction(action);
}
