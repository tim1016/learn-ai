import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { CohortActionResult } from '../lib/broker-v2-panel.types';

/**
 * What a batch actually did, per leg.
 *
 * The counts are always shown; the per-leg lines appear whenever a leg did
 * not apply, because "refused 1" without naming which bot leaves the operator
 * to diff the roster by hand. A refusal is alerted as loudly as a failure —
 * both mean the command did not happen.
 */
@Component({
  selector: 'app-cohort-archive-outcome',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="cohort-archive__outcome" [attr.role]="unresolved() ? 'alert' : 'status'">
      <strong>
        Archived {{ result().applied_count }}, already archived
        {{ result().replayed_count }}, refused {{ result().refused_count }}, failed
        {{ result().failed_count }}.
      </strong>
      @if (unresolved()) {
        <ul>
          @for (leg of result().legs; track leg.strategy_instance_id) {
            @if (leg.error; as error) {
              <li><code>{{ leg.strategy_instance_id }}</code> — {{ error.message }}</li>
            }
          }
        </ul>
      }
    </section>
  `,
  styleUrl: './cohort-archive-outcome.component.scss',
})
export class CohortArchiveOutcomeComponent {
  readonly result = input.required<CohortActionResult>();

  protected readonly unresolved = computed(
    () => this.result().refused_count + this.result().failed_count > 0,
  );
}
