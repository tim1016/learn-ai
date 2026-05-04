import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { describeError } from './error-catalog';
import { formatErrorDetails } from './error-display';

/**
 * Card-scoped error strip — used when one async section of a page
 * has failed (e.g. positions stream errored but account snapshot is
 * fine). Renders inside the affected card with a retry CTA and a
 * collapsible technical-details drawer.
 *
 * Errors are state, not events: this lives in the card until the
 * underlying condition resolves. Toasts are reserved for transient
 * confirmations like "order placed" or "snapshot saved".
 */
@Component({
  selector: 'app-section-error',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './section-error.component.scss',
  template: `
    @let info = display();
    @if (info) {
      <div class="section-error" role="alert">
        <div class="section-error-head">
          <span class="section-error-icon" aria-hidden="true">!</span>
          <div class="section-error-copy">
            <p class="section-error-what">{{ info.what }}</p>
            <p class="section-error-try">{{ info.tryCopy }}</p>
          </div>
          @if (canRetry()) {
            <button
              type="button"
              class="section-error-retry"
              (click)="retry.emit()"
              [disabled]="retrying()"
            >
              {{ retrying() ? 'Retrying…' : 'Retry' }}
            </button>
          }
        </div>
        @if (info.mathRef; as ref) {
          <p class="section-error-mathref">
            <a [href]="ref" target="_blank" rel="noopener">View math sources of truth ↗</a>
          </p>
        }
        @if (details()) {
          <details class="section-error-details">
            <summary>Show technical details</summary>
            <pre>{{ details() }}</pre>
          </details>
        }
      </div>
    }
  `,
})
export class SectionErrorComponent {
  readonly error = input<unknown>(null);
  readonly contextWhat = input<string | undefined>(undefined);
  readonly canRetry = input(true);
  readonly retrying = input(false);
  readonly retry = output();

  readonly display = computed(() => {
    const err = this.error();
    if (!err) return null;
    return describeError(err, this.contextWhat());
  });

  readonly details = computed(() => {
    const err = this.error();
    return err ? formatErrorDetails(err) : '';
  });
}
