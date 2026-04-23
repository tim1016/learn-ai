import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/**
 * App-wide page header. Standardizes the h1 + subtitle + optional eyebrow /
 * actions slot so every route renders the same hierarchy, spacing, and
 * WCAG-AA subtitle contrast.
 *
 * Usage:
 *   <app-page-header title="Engine Lab" subtitle="…">
 *     <button slot="actions">…</button>
 *   </app-page-header>
 */
@Component({
  selector: 'app-page-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './page-header.component.scss',
  template: `
    <header class="page-header">
      <div class="page-header-inner">
        @if (eyebrow()) {
          <span class="eyebrow">{{ eyebrow() }}</span>
        }
        <h1 class="page-title">{{ title() }}</h1>
        @if (subtitle()) {
          <p class="page-subtitle">{{ subtitle() }}</p>
        }
      </div>
      <div class="page-header-actions">
        <ng-content select="[slot=actions]" />
      </div>
    </header>
  `,
})
export class PageHeaderComponent {
  readonly title = input.required<string>();
  readonly subtitle = input<string | undefined>(undefined);
  readonly eyebrow = input<string | undefined>(undefined);
}
