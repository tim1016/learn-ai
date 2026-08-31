import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import type { CohortArchiveCohort, CohortArchiveLeg } from '../lib/broker-v2-panel.types';

/**
 * One (strategy, symbol) group of archivable bots.
 *
 * Extracted from the drawer so its template stays inside the repository's
 * ~80-line limit, and so the leg row — the part with the real rendering rules
 * (canonical symbol identity, a disabled leg that must still show its reason)
 * — has one home rather than living inside a long parent template.
 */
@Component({
  selector: 'app-cohort-archive-group',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent],
  templateUrl: './cohort-archive-group.component.html',
  styleUrl: './cohort-archive-group.component.scss',
})
export class CohortArchiveGroupComponent {
  readonly cohort = input.required<CohortArchiveCohort>();
  readonly selected = input.required<ReadonlySet<string>>();

  readonly toggled = output<CohortArchiveLeg>();

  protected isSelected(leg: CohortArchiveLeg): boolean {
    return this.selected().has(leg.strategy_instance_id);
  }
}
