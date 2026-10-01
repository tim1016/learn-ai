import { ChangeDetectionStrategy, Component, input, output, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import type { RetainKind } from './golden-search.types';

export interface RetainDecision {
  readonly kind: RetainKind;
  readonly note: string;
}

/** The ways to finish without a new golden configuration, in operator copy. */
export const RETAIN_KINDS: readonly { kind: RetainKind; label: string; hint: string }[] = [
  { kind: 'keep_current', label: 'Keep current settings', hint: 'The incumbent stays the default.' },
  { kind: 'wait_for_fresh_data', label: 'Wait for fresh data', hint: 'Revisit when new sessions can serve as an untouched test.' },
  { kind: 'retain_exploration', label: 'Retain as exploration', hint: 'Keep the evidence on record without using it.' },
];

/**
 * "Keep current settings" and the other ways to finish a study without a
 * new golden configuration (#2696). It opens in place so the decision is a
 * deliberate second step, and records an optional reason with it.
 */
@Component({
  selector: 'app-golden-search-retain',
  imports: [ButtonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-retain.component.html',
  styleUrl: './golden-search-retain.component.scss',
})
export class GoldenSearchRetainComponent {
  readonly busy = input(false);
  readonly decide = output<RetainDecision>();

  protected readonly kinds = RETAIN_KINDS;
  readonly open = signal(false);
  readonly kind = signal<RetainKind>('keep_current');
  readonly note = signal('');

  protected toggle(): void {
    this.open.update((open) => !open);
  }

  protected onKind(kind: RetainKind): void {
    this.kind.set(kind);
  }

  protected onNote(event: Event): void {
    if (event.target instanceof HTMLTextAreaElement) this.note.set(event.target.value);
  }

  protected record(): void {
    this.decide.emit({ kind: this.kind(), note: this.note().trim() });
  }
}
