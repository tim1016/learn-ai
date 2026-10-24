import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import type { DataProvenance } from './data-provenance';

/**
 * "Sample data — not real", drawn whenever the dataset behind a view says it
 * is `sample` and nothing at all when it is `real`. Bind it to the provenance
 * of the data being rendered, in every surface that renders it (a page and each
 * drawer it opens), so replacing one panel with real data never silences the
 * label on the panels that are still synthetic.
 */
@Component({
  selector: 'app-sample-data-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (provenance() === 'sample') {
      <p class="sample-data-banner" role="note" data-testid="sample-data-banner">
        <strong>Sample data — not real</strong>
        <span>Generated for demonstration. Not measured from any market, and never approval or deployment evidence.</span>
      </p>
    }
  `,
  styles: `
    :host { display: block; min-width: 0; }

    .sample-data-banner {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-1) var(--space-3);
      align-items: baseline;
      margin: 0 0 var(--space-3);
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--warn);
      border-radius: var(--radius-md);
      background: var(--warn-soft);
      color: var(--text-primary);
      font-size: var(--fs-xs);
      line-height: 1.4;

      strong { font-weight: var(--fw-semi); text-transform: uppercase; letter-spacing: 0.04em; }
    }
  `,
})
export class SampleDataBannerComponent {
  readonly provenance = input.required<DataProvenance>();
}
