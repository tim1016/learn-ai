import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';

import type { OperatorBlocker } from '../../../../api/operator-blocker.types';
import type { PanelAction, PanelActionTrigger } from '../lib/broker-v2-panel.types';
import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import { formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';

export type PanelActionTone = 'neutral' | 'warning' | 'danger';

let nextBlockerDescriptionId = 0;

function describeBlocker(blocker: OperatorBlocker): string {
  return [formatReceiptLabel(blocker.condition.id), blocker.headline, blocker.detail]
    .filter((part): part is string => Boolean(part))
    .join(' — ');
}

/** Renders one backend-presented panel action with its confirmation and blockers. */
@Component({
  selector: 'app-panel-action-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    TypedHaltConfirmComponent,
  ],
  templateUrl: './panel-action-button.component.html',
  styleUrl: './panel-action-button.component.scss',
})
export class PanelActionButtonComponent {
  readonly action = input.required<PanelAction>();
  readonly pending = input(false);
  readonly tone = input<PanelActionTone>('neutral');
  /** The bot page's toolbar (#2794): a PrimeIcons name drawn before the label. */
  readonly icon = input<string | null>(null);
  /** The page's plain name for the action, in place of the backend's system label. */
  readonly label = input<string | null>(null);
  /** When false the label is for screen readers and the tooltip only: an icon button. */
  readonly showLabel = input(true);
  /** The page's primary: filled in its own tone, so a filled danger action stays red. */
  readonly filled = input(false);
  /** The page's own tooltip -- what the action does, or why it waits -- in place of the backend's system copy. */
  readonly hint = input<string | null>(null);
  /** The host opens this action's own ticket, which asks for more than a yes and
   * confirms there (#2854): a press goes straight to the host, with no dialog here. */
  readonly opensTicket = input(false);

  readonly triggered = output<PanelActionTrigger>();
  protected readonly confirmationOpen = signal(false);
  protected readonly blockerDescriptionId = `panel-action-blocker-${nextBlockerDescriptionId++}`;

  protected readonly shownLabel = computed(() => this.label() ?? this.action().label);

  protected readonly visibleBlockers = computed(() => this.action().blockers);

  /**
   * Why the button is disabled, as a hover tooltip and an
   * `aria-describedby` target — not stacked prose under the button, which
   * inflated it to the point of misaligning it with the overflow trigger
   * beside it.
   */
  protected readonly tooltip = computed(() => {
    const hint = this.hint();
    if (hint !== null) return hint;
    const blockers = this.visibleBlockers();
    return blockers.length === 0
      ? this.action().explanation
      : blockers.map(describeBlocker).join('\n');
  });

  protected readonly disabled = computed(
    () => !this.action().enabled || this.pending(),
  );

  private readonly button = viewChild.required<ElementRef<HTMLButtonElement>>('button');

  /** Moves the keyboard to the action's button, e.g. when the ticket it opened closed by itself (#2854). */
  focus(): void {
    this.button().nativeElement.focus();
  }

  protected trigger(): void {
    if (this.disabled()) return;
    if (this.action().confirmation && !this.opensTicket()) {
      this.confirmationOpen.set(true);
      return;
    }
    this.triggered.emit({ action: this.action(), reason: null });
  }

  protected confirm(): void {
    this.confirmationOpen.set(false);
    this.triggered.emit({ action: this.action(), reason: null });
  }
}
