import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { InstrumentContextService } from '../instrument-context.service';
import { ReturnsDistributionComponent } from './returns-distribution.component';

/** The one timeframe the study is built on: one move per trading day. */
const DAILY_TIMEFRAME = '1d';

/**
 * An instrument's Returns tab: the Return Distribution study over the
 * committed window (PRD #3490 R09, R10).
 *
 * The study is daily by construction (Python splits each trading day at the
 * open and the close, from the lake's whole-day minute bars), so it cannot run
 * on another timeframe. A context that names one is said so before anything
 * runs, with the committed window and the choice left as they are; moving to
 * the daily timeframe is the reader's own explicit action. It never runs on
 * the timeframe it was not asked for, and the session setting does not change
 * it either, which the tab says.
 */
@Component({
  selector: 'app-instrument-returns',
  imports: [ReturnsDistributionComponent, WorkspaceStateBlockComponent],
  templateUrl: './instrument-returns.component.html',
  styleUrl: './instrument-returns.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InstrumentReturnsComponent {
  private readonly context = inject(InstrumentContextService);

  protected readonly timeframe = computed(() => this.context.shown().timeframe);

  /** Why the study will not run on this context, in place of it; `null` when it can. */
  protected readonly refusal = computed<WorkspaceStateContent | null>(() => {
    const timeframe = this.timeframe();
    if (timeframe === DAILY_TIMEFRAME) return null;
    return {
      kind: 'invalid',
      happened: `Returns does not run on ${timeframe} bars, so nothing was run.`,
      why: 'The study measures one move per trading day, from the whole-day minute bars the lake holds.',
      next: 'Use the 1 day timeframe for Returns, or stay on the Chart tab for this one.',
      actions: [{ id: 'use-daily', label: 'Use the 1 day timeframe' }],
    };
  });

  protected readonly sessionNote = computed(() =>
    this.context.shown().session === 'extended'
      ? 'Returns always splits the whole trading day at the open and the close, so the Extended hours setting changes nothing here.'
      : null,
  );

  protected onAction(id: string): void {
    if (id === 'use-daily') this.context.commitTimeframe(DAILY_TIMEFRAME);
  }
}
