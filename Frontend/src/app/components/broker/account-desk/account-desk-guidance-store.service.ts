import { Injectable, computed, inject } from '@angular/core';

import {
  accountDeskAnchorOrVerdictFallback,
  operatorAttentionConditionCount,
  operatorBlockersForAccountDeskLens,
  type AccountDeskLens,
  type OperatorBlocker,
  type OperatorBlockerAnchorKind,
} from '../../../api/operator-blocker.types';
import { AccountDeskHoldingsStore } from './account-desk-holdings-store.service';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

/**
 * Projects backend-authored Account desk guidance to semantic UI anchors.
 * This is deliberately presentation-only: condition identity, prose, actions,
 * audience, and disposition all remain server-owned.
 */
@Injectable()
export class AccountDeskGuidanceStore {
  private readonly surface = inject(AccountDeskSurfaceStore);
  private readonly holdings = inject(AccountDeskHoldingsStore);

  private readonly blockers = computed<readonly OperatorBlocker[]>(() => {
    const triageBlockers = this.surface.triage()?.operator_blockers ?? [];
    return [...triageBlockers, ...this.holdings.operatorBlockers()]
      .flatMap(projectWithVerdictFallback);
  });

  readonly operatorAttentionCount = computed(() => operatorAttentionConditionCount(this.blockers()));

  blockersFor(
    anchor: OperatorBlockerAnchorKind,
    subjectKey: string | null,
    lens: AccountDeskLens,
  ): readonly OperatorBlocker[] {
    return deduplicateConditionProjections(
      operatorBlockersForAccountDeskLens(this.blockers(), lens).filter(
        (blocker) => blocker.anchor.kind === anchor && blocker.anchor.subject_key === subjectKey,
      ),
    );
  }
}

function projectWithVerdictFallback(blocker: OperatorBlocker): OperatorBlocker[] {
  const anchor = accountDeskAnchorOrVerdictFallback(blocker.anchor);
  return anchor === null ? [] : [{ ...blocker, anchor }];
}

function deduplicateConditionProjections(blockers: readonly OperatorBlocker[]): readonly OperatorBlocker[] {
  const seen = new Set<string>();
  return blockers.filter((blocker) => {
    if (seen.has(blocker.condition.id)) return false;
    seen.add(blocker.condition.id);
    return true;
  });
}
