import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { RunFailure } from '../options-backtest.types';
import { failureGroups, SMALL_GROUP, type FailureGroup } from './failure-view';

/**
 * Why a run failed, as the server says it: a reason that names no date, which
 * alone is announced, then what failed, grouped by code with a count and each
 * group opened on request (a small one is open already). A session is named by
 * its date, in New York dates, and the run as a whole by words.
 */
@Component({
  selector: 'app-options-backtest-run-failure',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-failure.component.html',
  styleUrl: './run-failure.component.scss',
})
export class RunFailureComponent {
  readonly failure = input.required<RunFailure>();

  protected readonly groups = computed(() => failureGroups(this.failure().items));
  /** The groups the owner opened or closed; any other is open when it is small. */
  private readonly chosen = signal<ReadonlyMap<string, boolean>>(new Map());

  protected isOpen(group: FailureGroup): boolean {
    return this.chosen().get(group.code) ?? group.items.length <= SMALL_GROUP;
  }

  protected toggle(group: FailureGroup): void {
    this.chosen.update((chosen) => new Map(chosen).set(group.code, !this.isOpen(group)));
  }

  protected listId(group: FailureGroup): string {
    return `ob-failure-${group.code}`;
  }
}
