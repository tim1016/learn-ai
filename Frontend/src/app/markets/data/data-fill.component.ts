import { ChangeDetectionStrategy, Component, computed, inject, input, output } from '@angular/core';

import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { formatReceiptLabel } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayPipe } from '../../shared/timestamp';
import { gapCauseText } from './gap-copy';
import type { FillReport } from './instrument-data';
import { InstrumentDataStore, type FillState } from './instrument-data.store';
import type { InstrumentDataReport } from '../../shared/data-lake';

/** The sentence for sessions still missing after a Fill, one cause per clause. */
function missingText(report: FillReport): string {
  return report.missing.map((cause) => `${cause.count}: ${gapCauseText(cause)}`).join(' ');
}

/**
 * The Data tab's two explicit actions, Check and Fill, and what a Fill did.
 *
 * Nothing here runs on its own. Check reads the lake again; Fill submits the
 * backfill job over the window Python named, and when the job ends the lake is
 * read again and the Fill is reported in sessions: added, reused, and still
 * missing with the cause of each. A job that stopped, or was cancelled, is
 * said so and never reads as complete, whatever it wrote before it stopped.
 */
@Component({
  selector: 'app-data-fill',
  imports: [WorkspaceStateBlockComponent, TimestampDisplayPipe],
  templateUrl: './data-fill.component.html',
  styleUrl: './data-fill.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataFillComponent {
  private readonly store = inject(InstrumentDataStore);

  readonly report = input.required<InstrumentDataReport>();
  readonly checked = output();

  protected readonly fill = this.store.fill;
  protected readonly filling = this.store.filling;
  protected readonly window = computed(() => this.report().fill_window ?? null);
  protected readonly fetchable = computed(() => this.report().summary.fetchable);

  /** Which window and tree the Fill on screen is for: it may not be the one being read now. */
  protected readonly scope = computed(() => {
    const fill = this.fill();
    if (fill.kind === 'idle') return null;
    const { symbol, from, to, tree } = fill.request;
    return `${symbol} · ${from} → ${to} · ${tree === 'raw' ? 'Raw' : 'Split-adjusted'}`;
  });

  /** The fill state, drawn in the words the shared state block gives every view. */
  protected readonly state = computed<WorkspaceStateContent | null>(() => describeFill(this.fill(), this.store.progress()));

  protected check(): void {
    this.checked.emit();
  }

  protected startFill(): void {
    void this.store.startFill();
  }

  protected onAction(id: string): void {
    if (id === 'cancel-fill') void this.store.cancelFill();
    else if (id === 'dismiss-fill') this.store.dismissFill();
  }
}

function describeFill(
  fill: FillState,
  progress: { readonly done: number; readonly total: number } | null,
): WorkspaceStateContent | null {
  switch (fill.kind) {
    case 'idle':
      return null;
    case 'running':
      return {
        kind: 'filling',
        happened: `Filling ${fill.sessions} ${fill.sessions === 1 ? 'session' : 'sessions'} of ${fill.request.symbol}.`,
        progress: progress === null ? { kind: 'phase', label: 'Starting the backfill job' } : { kind: 'counts', done: progress.done, total: progress.total, unit: 'days of the span' },
        next: 'Cancelling stops the job; what it already wrote stays in the lake. You can leave this tab: the job keeps going.',
        actions: [{ id: 'cancel-fill', label: 'Cancel the fill' }],
      };
    case 'ended':
      return describeEnd(fill);
  }
}

function describeEnd(fill: Extract<FillState, { kind: 'ended' }>): WorkspaceStateContent {
  const dismiss = [{ id: 'dismiss-fill', label: 'Dismiss' }];
  const { end, report } = fill;
  if (report === null) {
    return {
      kind: 'unavailable',
      happened: 'The fill ended, but the lake could not be read again, so what it added is not known.',
      why: fill.rereadProblem,
      next: 'Check again to count what the lake holds now.',
      actions: dismiss,
    };
  }
  const counts = `${report.added} added, ${report.reused} already held`;
  if (end.kind === 'failed') {
    const cancelled = end.code === 'backfill_cancelled';
    return {
      kind: cancelled ? 'cancelled' : 'failed',
      happened: `${cancelled ? 'The fill was cancelled' : 'The fill stopped before it finished'}: ${counts}, ${report.missingCount} still missing.`,
      why: cancelled ? null : `${formatReceiptLabel(end.code)}: ${end.message}`,
      next: report.missingCount > 0 ? `What it wrote stays. Still missing: ${missingText(report)}` : null,
      actions: dismiss,
    };
  }
  if (report.complete) {
    return { kind: 'complete', happened: `Fill finished: ${counts}, none missing. A run can read every finished session.`, actions: dismiss };
  }
  return {
    kind: 'partial',
    happened: `Fill finished: ${counts}, ${report.missingCount} still missing.`,
    why: missingText(report),
    actions: dismiss,
  };
}
