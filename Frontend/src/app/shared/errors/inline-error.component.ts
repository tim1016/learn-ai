import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { describeError } from './error-catalog';

/**
 * One-line red error for a single form field or control.
 *
 * Intentionally has no retry button and no technical-details
 * drawer — those concerns belong on the section / page variants.
 * Use this for validation errors and single-control failures
 * where the *what* is short and the *try* is implicit (fix the
 * input).
 */
@Component({
  selector: 'app-inline-error',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './inline-error.component.scss',
  template: `
    @if (display()) {
      <p class="inline-error" role="alert">
        <span class="inline-error-icon" aria-hidden="true">!</span>
        <span class="inline-error-text">{{ display() }}</span>
      </p>
    }
  `,
})
export class InlineErrorComponent {
  readonly error = input<unknown>(null);
  readonly message = input<string | undefined>(undefined);

  readonly display = computed(() => {
    const explicit = this.message();
    if (explicit) return explicit;
    const err = this.error();
    if (!err) return null;
    return describeError(err).what;
  });
}
