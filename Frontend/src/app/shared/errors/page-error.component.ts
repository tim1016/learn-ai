import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { describeError } from './error-catalog';
import { formatErrorDetails } from './error-display';

/**
 * Whole-page error state — shown in place of the page body when
 * the route itself failed (resolver error, GraphQL 500, broker
 * disconnected for a broker-only page). Larger and more emphatic
 * than the section variant; carries the same what / try / details
 * shape so users learn one mental model.
 */
@Component({
  selector: 'app-page-error',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './page-error.component.scss',
  template: `
    @let info = display();
    @if (info) {
      <section class="page-error" role="alert" aria-live="assertive">
        <header class="page-error-head">
          @if (eyebrow()) {
            <span class="page-error-eyebrow">{{ eyebrow() }}</span>
          }
          <h2 class="page-error-title">{{ info.what }}</h2>
          <p class="page-error-try">{{ info.tryCopy }}</p>
        </header>
        <div class="page-error-actions">
          @if (canRetry()) {
            <button
              type="button"
              class="page-error-retry"
              (click)="retry.emit()"
              [disabled]="retrying()"
            >
              {{ retrying() ? 'Retrying…' : 'Retry' }}
            </button>
          }
          @if (info.mathRef; as ref) {
            <a class="page-error-mathref" [href]="ref" target="_blank" rel="noopener">
              View math sources of truth ↗
            </a>
          }
        </div>
        @if (details()) {
          <details class="page-error-details">
            <summary>Show technical details</summary>
            <pre>{{ details() }}</pre>
          </details>
        }
      </section>
    }
  `,
})
export class PageErrorComponent {
  readonly error = input<unknown>(null);
  readonly contextWhat = input<string | undefined>(undefined);
  readonly eyebrow = input<string | undefined>(undefined);
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
