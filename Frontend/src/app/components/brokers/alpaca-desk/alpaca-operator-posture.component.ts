import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
} from '@angular/core';
import { Router } from '@angular/router';

import type {
  SqliteClerkProjection,
  SqliteRecoveryAction,
} from '../../../api/alpaca.types';
import type { OperatorBlocker, OperatorMove } from '../../../api/operator-blocker.types';

type PostureDisposition = OperatorBlocker['disposition'] | 'healthy' | 'review';

interface OperatorPostureView {
  readonly headline: string;
  readonly detail: string;
  readonly disposition: PostureDisposition;
  readonly action: SqliteRecoveryAction | OperatorMove | null;
  readonly nextStep: string | null;
}

/**
 * One decision-first operator posture. `OperatorBlocker` remains authoritative
 * when a caller has one; active SQLite custody uses its own server-authored
 * guidance and recovery capability until that broader contract is available on
 * the Alpaca account endpoint.
 */
@Component({
  selector: 'app-alpaca-operator-posture',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './alpaca-operator-posture.component.html',
  styleUrl: './alpaca-operator-posture.component.scss',
})
export class AlpacaOperatorPostureComponent {
  private readonly router = inject(Router, { optional: true });
  readonly blocker = input<OperatorBlocker | null>(null);
  readonly projection = input<SqliteClerkProjection | null>(null);

  readonly repairRequested = output<SqliteRecoveryAction>();
  readonly blockerMoveRequested = output<OperatorMove>();

  protected readonly posture = computed<OperatorPostureView | null>(() => {
    const blocker = this.blocker();
    if (blocker !== null) return blockerPosture(blocker);

    const projection = this.projection();
    if (projection === null) return null;
    return projectionPosture(projection);
  });

  protected readonly isRecoveryAction = isRecoveryAction;

  protected requestRepair(action: SqliteRecoveryAction): void {
    this.repairRequested.emit(action);
  }

  protected requestBlockerMove(move: OperatorMove): void {
    this.blockerMoveRequested.emit(move);
  }

  protected requestMove(action: SqliteRecoveryAction | OperatorMove | null): void {
    if (isRecoveryAction(action) || action === null) return;
    if (action.action.kind === 'navigate' && this.router !== null) {
      void this.router.navigate([action.action.route], {
        fragment: action.action.fragment ?? undefined,
      });
      return;
    }
    this.requestBlockerMove(action);
  }
}

function blockerPosture(blocker: OperatorBlocker): OperatorPostureView {
  return {
    headline: blocker.headline,
    detail: blocker.detail ?? '',
    disposition: blocker.disposition,
    action: blocker.primary_move,
    nextStep: blocker.primary_move?.target ?? null,
  };
}

function projectionPosture(projection: SqliteClerkProjection): OperatorPostureView {
  const guidance = projection.guidance;
  const primaryAction = projection.recovery_actions.find((action) => action.primary) ?? null;
  const isHealthy = !guidance.action_required
    && projection.uncertainties.length === 0
    && projection.authority_health === 'healthy';
  if (isHealthy) {
    return {
      headline: guidance.headline,
      detail: guidance.explanation,
      disposition: 'healthy',
      action: null,
      nextStep: guidance.next_step,
    };
  }
  if (primaryAction?.available) {
    return {
      headline: guidance.headline,
      detail: guidance.explanation,
      disposition: 'fix_here',
      action: primaryAction,
      nextStep: primaryAction.next_step,
    };
  }
  if (primaryAction !== null) {
    return {
      headline: guidance.headline,
      detail: primaryAction.unavailable_reason ?? guidance.explanation,
      disposition: guidance.action_required ? 'wait' : 'review',
      action: null,
      nextStep: primaryAction.next_step,
    };
  }
  if (!guidance.action_required) {
    return {
      headline: guidance.headline,
      detail: guidance.explanation,
      disposition: 'review',
      action: null,
      nextStep: guidance.next_step,
    };
  }
  return {
    headline: guidance.headline,
    detail: guidance.impact,
    disposition: 'terminal',
    action: null,
    nextStep: guidance.next_step,
  };
}

export function isRecoveryAction(
  action: SqliteRecoveryAction | OperatorMove | null,
): action is SqliteRecoveryAction {
  return action !== null && 'action_id' in action;
}
