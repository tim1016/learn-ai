import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { operationsTabLabel, operationsTabRoute } from '../../operations/operations-tabs';
import { SEVERITY_WORD, type AffectedOperation } from './scoped-health';
import { watchScopedHealth } from './scoped-health.service';

/**
 * One line on a page about the health of what the page depends on, with a link
 * to the Operations tab and filter that show the evidence (PRD #3490 R30).
 *
 * The page says what it does (`needs`, and `instrument` where it reads one
 * symbol); the data plane says what that is affected by. A page nothing
 * affects shows nothing, and a failure of the projection itself shows as
 * unknown, never as a clean page.
 *
 * A notice is a summary, not a gate: whatever refuses an action keeps its own
 * reason at that action.
 */
@Component({
  selector: 'app-health-notice',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './health-notice.component.html',
  styleUrl: './health-notice.component.scss',
})
export class HealthNoticeComponent {
  /** The operations this page does, in the projection's terms (`HEALTH_NEEDS`). */
  readonly needs = input.required<readonly AffectedOperation[]>();
  /** The one symbol the page reads, when it reads one: a lake gap in another symbol is not its business. */
  readonly instrument = input<string | null>(null);
  /** What only the page knows, said after the finding. */
  readonly note = input<string | null>(null);

  /** Read again on the chips' own cadence, so the notice and the chips never disagree for long. */
  private readonly projection = watchScopedHealth(() => ({ operations: this.needs(), instrument: this.instrument() }));

  /** The projection could not be read: nothing can be said healthy. */
  protected readonly unreadable = computed(() => this.projection.error() !== undefined);

  /** The worst finding the data plane says deserves a note, and how many others there are. */
  protected readonly finding = computed(() => {
    // No answer yet, or the answer to another symbol was dropped when the page's changed: say nothing until it answers.
    if (!this.projection.hasValue()) return null;
    const [worst, ...rest] = this.projection.value().items.filter((item) => item.needs_attention);
    if (worst === undefined) return null;
    return {
      word: SEVERITY_WORD[worst.severity],
      severity: worst.severity,
      text: worst.explanation,
      others: rest.length,
      link: worst.link === null || worst.link === undefined ? null : {
        label: operationsTabLabel(worst.link.tab),
        route: operationsTabRoute(worst.link.tab),
        query: worst.link.query ?? {},
        fragment: worst.link.fragment ?? undefined,
      },
    };
  });

  protected readonly operationsRoute = operationsTabRoute('lake');
}
