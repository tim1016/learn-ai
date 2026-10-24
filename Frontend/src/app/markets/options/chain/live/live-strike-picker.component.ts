import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

/** The strike chip's label: a whole strike as its number, a part strike with its cents, so 717.5 is never read as 718. */
export function strikeLabel(strike: number): string {
  return Number.isInteger(strike) ? String(strike) : strike.toFixed(2);
}

/**
 * The strikes IBKR can qualify for the expiry on screen, as chips to subscribe to. It lists and
 * reports what the operator picks; it subscribes to nothing, and picking a strike starts no line.
 */
@Component({
  selector: 'app-live-strike-picker',
  templateUrl: './live-strike-picker.component.html',
  styleUrl: './live-strike-picker.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LiveStrikePickerComponent {
  readonly strikes = input.required<readonly number[]>();
  readonly selected = input.required<ReadonlySet<number>>();
  readonly loading = input(false);
  /** Strikes within this many dollars of the spot are what the band picks. */
  readonly bandDollars = input.required<number>();
  /** Why the band cannot be picked yet, or null when it can. */
  readonly bandUnavailable = input<string | null>(null);

  readonly toggled = output<number>();
  readonly bandPicked = output();
  readonly cleared = output();

  protected readonly chips = computed(() => this.strikes().map((strike) => ({ strike, label: strikeLabel(strike) })));
}
