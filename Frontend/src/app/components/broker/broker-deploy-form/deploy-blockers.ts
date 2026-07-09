import type { OperatorBlocker, OperatorMove } from '../../../api/operator-blocker.types';

export interface FormBlockerInput {
  missingRequiredFields: string[];
  identityConflictSummary: string | null;
  exposureConflictSummary: string | null;
  customSizingError: string | null;
  allInCoexistenceBlock?: string | null;
  liveExecutionSelected?: boolean;
  actionPlanReady: boolean;
  actionPlanMessage?: string | null;
}

function confirmInForm(label: string, anchor: string): OperatorMove {
  return { label, action: { kind: 'confirm_in_form', anchor }, target: null };
}

export function buildFormBlockers(input: FormBlockerInput): OperatorBlocker[] {
  const blockers: OperatorBlocker[] = [];

  if (input.missingRequiredFields.length > 0) {
    blockers.push({
      id: 'missing_required_fields',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Deployment details incomplete',
      detail: `Missing: ${input.missingRequiredFields.join(', ')}.`,
      primary_move: confirmInForm('Complete the form', 'strategy-section'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  if (input.identityConflictSummary !== null) {
    blockers.push({
      id: 'identity_coherence_unconfirmed',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Run identity needs confirmation',
      detail: input.identityConflictSummary,
      primary_move: confirmInForm('Confirm identity', 'identity-coherence-card'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  if (input.exposureConflictSummary !== null) {
    blockers.push({
      id: 'exposure_coherence_unconfirmed',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Exposure needs confirmation',
      detail: input.exposureConflictSummary,
      primary_move: confirmInForm('Confirm exposure', 'exposure-launch-decision'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  if (input.customSizingError !== null) {
    blockers.push({
      id: 'sizing_invalid',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Sizing is invalid',
      detail: input.customSizingError,
      primary_move: confirmInForm('Fix sizing', 'sizing-section'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  if (input.allInCoexistenceBlock) {
    blockers.push({
      id: 'reference_parity_coexistence_blocked',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Reference parity sizing is blocked',
      detail: input.allInCoexistenceBlock,
      primary_move: confirmInForm('Pick a different sizing preset', 'sizing-section'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  if (input.liveExecutionSelected) {
    blockers.push({
      id: 'live_execution_unavailable',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Live execution is unavailable from Deploy',
      detail: 'Pick read-only observation or paper orders.',
      primary_move: confirmInForm('Pick paper or read-only', 'launch-section'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  if (!input.actionPlanReady) {
    blockers.push({
      id: 'action_plan_incomplete',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Entry/exit legs incomplete',
      detail: input.actionPlanMessage ?? 'Add a valid entry leg and a matching close leg before deploying.',
      primary_move: confirmInForm('Fix the legs', 'action-plan-picker-heading'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  return blockers;
}

export function deployReady(blockers: OperatorBlocker[]): boolean {
  return blockers.every((b) => b.severity !== 'blocking');
}

export interface MoveDispatch {
  navigate(route: string, fragment: string | null): void;
  focusAnchor(anchor: string): void;
}

export interface RenderedMove {
  label: string;
  variant: 'primary' | 'link';
  invoke(): void;
}

export function resolveBlockerMove(move: OperatorMove, deps: MoveDispatch): RenderedMove | null {
  switch (move.action.kind) {
    case 'navigate': {
      const { route, fragment } = move.action;
      return {
        label: move.label,
        variant: 'link',
        invoke: () => deps.navigate(route, fragment),
      };
    }
    case 'confirm_in_form': {
      const { anchor } = move.action;
      return {
        label: move.label,
        variant: 'primary',
        invoke: () => deps.focusAnchor(anchor),
      };
    }
    default:
      return null;
  }
}
