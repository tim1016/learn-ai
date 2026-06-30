// PRD #617 — pure mapping from GateSuggestedAction → operator label +
// dispatch handler.
//
// Destructive actions (Stop, Flatten-and-pause, Mark Poisoned) reach
// the operator only via `focus_action`, which is a *navigation hint*
// to the canonical render site, never an inline invocation
// (ADR-0010 §A2, ADR-0013 §1).  An unknown `kind` fails closed
// visibly: the renderer returns `null` and the cockpit shows the raw
// gate name and the documented unavailable reason instead of guessing.

import type {
  GateSuggestedAction,
  InvokeCapabilityAction,
  InvokeEndpointAction,
  FocusAction,
  OpenRunbookAction,
  TraderPrimaryRemediation,
} from '../../../../api/live-instances.types';
import type { InnerTab } from './instance-tab-state';

export interface RendererDispatch {
  invokeCapability(capability: InvokeCapabilityAction['capability']): void;
  focus(tab: InnerTab, action: FocusAction['action']): void;
  redeploy(): void;
  openRunbook(slug: string): void;
  invokeEndpoint?(endpoint: InvokeEndpointAction['endpoint']): void;
}

export interface RenderedAction {
  label: string;
  /** Either `'primary'` (filled CTA) or `'link'` (text-link visual). */
  variant: 'primary' | 'link';
  invoke(): void;
}

const _INVOKE_CAPABILITY_LABELS: Record<InvokeCapabilityAction['capability'], string> = {
  resume: 'Resume',
  pause: 'Pause',
};

const _FOCUS_LABELS: Record<FocusAction['action'], string> = {
  flatten_and_pause: 'Flatten and pause →',
  stop: 'Stop instance →',
  mark_poisoned: 'Mark poisoned →',
};

const _INVOKE_ENDPOINT_LABELS: Record<InvokeEndpointAction['endpoint'], string> = {
  reconcile_instance: 'Reconcile now',
};

export function renderSuggestedAction(
  action: GateSuggestedAction | TraderPrimaryRemediation | null,
  dispatch: RendererDispatch,
): RenderedAction | null {
  if (action === null) {
    return null;
  }
  switch (action.kind) {
    case 'invoke_capability':
      return {
        label: _INVOKE_CAPABILITY_LABELS[action.capability] ?? action.capability,
        variant: 'primary',
        invoke: () => dispatch.invokeCapability(action.capability),
      };
    case 'focus_action':
      return {
        label: _FOCUS_LABELS[action.action] ?? `Open ${action.action}`,
        variant: 'link',
        invoke: () => dispatch.focus(action.tab as InnerTab, action.action),
      };
    case 'redeploy':
      return {
        label: 'Redeploy →',
        variant: 'link',
        invoke: () => dispatch.redeploy(),
      };
    case 'open_runbook':
      return {
        label: 'Open runbook →',
        variant: 'link',
        invoke: () => dispatch.openRunbook((action as OpenRunbookAction).slug),
      };
    case 'invoke_endpoint': {
      if (!dispatch.invokeEndpoint) {
        return null;
      }
      const invokeEndpoint = dispatch.invokeEndpoint;
      return {
        label: _INVOKE_ENDPOINT_LABELS[action.endpoint],
        variant: 'primary',
        invoke: () => invokeEndpoint(action.endpoint),
      };
    }
    case 'none':
      return null;
    default: {
      // Unknown kind — fail closed visibly.  The cockpit's caller
      // renders the raw gate name + the documented unavailable
      // reason rather than a guessed remediation.
      return null;
    }
  }
}
