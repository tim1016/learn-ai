import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { exposurePreview, percentInputValue, studyTradeFloor } from './golden-search-display';
import type { StudyStep } from './golden-search-steps';
import type { EvidenceCandidate, FitToTest, StudyCommand, StudyDetail } from './golden-search.types';

/** A fit the final test will run, as the server names it, with its development evidence. */
export interface LockFit extends FitToTest {
  readonly candidate: EvidenceCandidate;
}

/**
 * The final-test lock (#2696, #2930), before the held-back interval is
 * opened: what the one look will consume (the interval, every fit that
 * differs from the current settings and the frozen incumbent, nothing else),
 * the frozen rules each fit will be judged by, the exposure the server's
 * ledger says opening would record, and an explicit acknowledgement before
 * "Open final test". Opening is irreversible; the server records the
 * exposure when it opens. Which fit to approve is chosen afterwards.
 */
@Component({
  selector: 'app-golden-search-lockbox',
  imports: [ButtonModule, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-lockbox.component.html',
  styleUrl: './golden-search-lockbox.component.scss',
})
export class GoldenSearchLockboxComponent {
  readonly study = input.required<StudyDetail>();
  /** The fits that will run, as the server lists them. */
  readonly fits = input.required<readonly LockFit[]>();
  readonly busy = input(false);
  readonly studyCommand = output<StudyCommand>();
  readonly goTo = output<StudyStep>();

  readonly acknowledged = signal(false);

  protected readonly ceiling = computed(() => `${percentInputValue(this.study().protocol.policy.max_drawdown_ceiling)}%`);
  protected readonly tradeFloor = computed(() => studyTradeFloor(this.study(), 'final'));
  /** The exposure opening would record, when the server previewed it. */
  protected readonly exposure = computed(() => exposurePreview(this.study()));
  protected readonly canOpen = computed(() => this.study().permitted_actions.includes('open_exam'));
  protected readonly refusal = computed(() => (this.canOpen() ? null : (this.study().action_refusals.open_exam ?? null)));
  protected readonly names = computed(() => this.fits().map((fit) => fit.label).join(', '));
  protected readonly warned = computed(() => this.fits().filter((fit) => fit.candidate.flags.length > 0));

  protected onAcknowledge(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.acknowledged.set(event.target.checked);
  }

  protected open(): void {
    if (!this.acknowledged() || !this.canOpen()) return;
    this.studyCommand.emit({ command: 'open_exam', payload: { acknowledge_final_test: true } });
  }
}
