import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/**
 * A strategy's registry warning, shown loud and as-is wherever the strategy is
 * chosen or runs (#2607, owner decision 7): "Experimental validation only —
 * not a trading strategy" for Deployment Validation. The words are the
 * backend's (`experimental_notice` on the registry entry); this only draws
 * them, always visible, never behind a hover.
 */
@Component({
  selector: 'app-experimental-notice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<p class="experimental-notice" role="note"><span aria-hidden="true">⚠</span> <strong>{{ notice() }}</strong></p>`,
  styles: `
    :host { display: block; min-width: 0; }

    .experimental-notice {
      margin: 0;
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--warn);
      border-radius: var(--radius-md);
      background: var(--warn-soft);
      color: var(--text-primary);
      font-size: var(--fs-xs);
      line-height: 1.4;

      span { margin-right: 0.3em; color: var(--warn); }
      strong { font-weight: var(--fw-semi); }
    }
  `,
})
export class ExperimentalNoticeComponent {
  readonly notice = input.required<string>();
}
