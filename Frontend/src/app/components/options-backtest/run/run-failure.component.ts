import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { RunFailure } from '../options-backtest.types';
import { failureGroups, SMALL_GROUP, type FailureGroup } from './failure-view';

let nextFailureId = 0;

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

  /** Two failures can be on one page (the Backtest tab keeps its run while History shows the same one), so a list's id is this instance's own. */
  private readonly instance = nextFailureId++;
  protected readonly groups = computed(() => failureGroups(this.failure().items));
  /** The groups the owner opened or closed; any other is open when it is small. */
  private readonly chosen = signal<ReadonlyMap<string, boolean>>(new Map());

  protected isOpen(group: FailureGroup): boolean {
    return this.chosen().get(group.code) ?? group.items.length <= SMALL_GROUP;
  }

  protected toggle(group: FailureGroup): void {
    this.chosen.update((chosen) => new Map(chosen).set(group.code, !this.isOpen(group)));
  }

  /** A group's code is whatever the server wrote, with spaces if it likes, so the id is made from the group's place and not from its code. */
  protected listId(position: number): string {
    return `ob-failure-${this.instance}-${position}`;
  }
}
