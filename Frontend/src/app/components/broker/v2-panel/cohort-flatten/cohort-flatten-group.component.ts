import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import type { CohortFlattenCohort, CohortFlattenLeg } from '../lib/broker-v2-panel.types';
import { COHORT_FLATTEN_COPY } from './cohort-flatten-confirmation';

/**
 * One (strategy, symbol) cohort and its member legs.
 *
 * A wave is one cohort at a time (ADR 0051): only the active cohort's armed
 * legs are selectable, and another cohort becomes the wave only through its
 * own "Select this cohort" button — which arms iff the backend armed one of
 * its legs (ADR 0047). Selecting only chooses the wave; nothing is sent
 * before the typed confirmation.
 * A disabled leg stays on screen with its blocker; hiding it would read as a
 * cohort with fewer members than it has.
 */
@Component({
  selector: 'app-cohort-flatten-group',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent],
  templateUrl: './cohort-flatten-group.component.html',
  styleUrl: './cohort-flatten-group.component.scss',
})
export class CohortFlattenGroupComponent {
  readonly cohort = input.required<CohortFlattenCohort>();
  readonly active = input.required<boolean>();
  readonly selected = input.required<ReadonlySet<string>>();
  /** The wave holds as many legs as one POST may carry; no more can join. */
  readonly capReached = input(false);

  readonly toggled = output<CohortFlattenLeg>();
  readonly chosen = output();

  protected readonly copy = COHORT_FLATTEN_COPY;

  protected isSelected(leg: CohortFlattenLeg): boolean {
    return this.active() && this.selected().has(leg.strategy_instance_id);
  }
}
