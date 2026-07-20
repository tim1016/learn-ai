import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  input,
  signal,
} from '@angular/core';

/**
 * One shared copy affordance for the whole app. Replaces the hand-rolled
 * `navigator.clipboard` + local-signal patterns that had drifted apart
 * (launcher command, QuantConnect reference code, docs copy-blocks).
 *
 * Two variants:
 *  - `icon`   — a bare icon that reveals on hover/focus of a `.copyable`
 *               ancestor (or the button itself). Always in the DOM and
 *               keyboard-reachable so it passes AXE even while visually
 *               hidden at rest.
 *  - `button` — a labelled pill, always visible.
 *
 * On success the icon swaps to a check and confirms for ~1.6s. A blocked
 * clipboard surfaces an inline, screen-reader-announced fallback instead
 * of failing silently.
 */
@Component({
  selector: 'app-copy-button',
  templateUrl: './copy-button.component.html',
  styleUrl: './copy-button.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CopyButtonComponent implements OnDestroy {
  /** The exact text written to the clipboard. */
  readonly text = input.required<string>();
  /** Idle label (button variant) and default accessible name. */
  readonly label = input('Copy');
  /** Confirmation label shown briefly after a successful copy. */
  readonly copiedLabel = input('Copied');
  readonly variant = input<'icon' | 'button'>('icon');
  /** Explicit accessible name; falls back to the label/copied label. */
  readonly ariaLabel = input('');

  protected readonly copied = signal(false);
  protected readonly failed = signal(false);
  protected readonly accessibleName = computed(() =>
    this.ariaLabel() || (this.copied() ? this.copiedLabel() : this.label()),
  );

  private resetTimer: ReturnType<typeof setTimeout> | null = null;

  protected async copy(): Promise<void> {
    this.failed.set(false);
    try {
      if (typeof navigator === 'undefined' || !navigator.clipboard) {
        throw new Error('Clipboard API unavailable');
      }
      await navigator.clipboard.writeText(this.text());
      this.copied.set(true);
      this.scheduleReset();
    } catch {
      this.copied.set(false);
      this.failed.set(true);
    }
  }

  private scheduleReset(): void {
    if (this.resetTimer) {
      clearTimeout(this.resetTimer);
    }
    this.resetTimer = setTimeout(() => this.copied.set(false), 1600);
  }

  ngOnDestroy(): void {
    if (this.resetTimer) {
      clearTimeout(this.resetTimer);
    }
  }
}
