import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { percentInputValue } from './golden-search-display';
import type { StudyStep } from './golden-search-steps';
import type { EvidenceCandidate, StudyCommand, StudyDetail } from './golden-search.types';

/**
 * The final-test lock (#2696), before the held-back interval is opened: what
 * the one look will consume (the interval, the locked candidate and the
 * frozen incumbent, nothing else), the frozen rules it will be judged by, and
 * an explicit acknowledgement before "Open final test". Opening is
 * irreversible; the server records the exposure when it opens.
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
  readonly candidate = input.required<EvidenceCandidate | null>();
  readonly busy = input(false);
  readonly studyCommand = output<StudyCommand>();
  readonly goTo = output<StudyStep>();

  readonly acknowledged = signal(false);

  protected readonly ceiling = computed(() => `${percentInputValue(this.study().protocol.policy.max_drawdown_ceiling)}%`);
  protected readonly canOpen = computed(() => this.study().permitted_actions.includes('open_exam'));
  protected readonly refusal = computed(() => (this.canOpen() ? null : (this.study().action_refusals.open_exam ?? null)));

  protected onAcknowledge(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.acknowledged.set(event.target.checked);
  }

  protected open(): void {
    if (!this.acknowledged() || !this.canOpen()) return;
    this.studyCommand.emit({ command: 'open_exam', payload: { acknowledge_final_test: true } });
  }
}
