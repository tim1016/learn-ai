import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/**
 * App-wide page header. Standardizes the h1 + subtitle + optional eyebrow /
 * actions slot so every route renders the same hierarchy, spacing, and
 * WCAG-AA subtitle contrast.
 *
 * Slots:
 *   - ``[slot=actions]`` — chips, links, or buttons aligned to the right.
 *   - ``[slot=guide]``   — optional helper block that renders below the
 *     title row. Intended for ``<app-page-guide>`` but accepts arbitrary
 *     content so per-page bespoke guides keep working.
 *
 * Usage:
 *   <app-page-header title="Engine Lab" subtitle="…">
 *     <button slot="actions">…</button>
 *     <app-page-guide slot="guide" pulls="…" why="…" />
 *   </app-page-header>
 */
@Component({
  selector: 'app-page-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './page-header.component.scss',
  template: `
    <header class="page-header" [class.page-header--with-tabs]="hasTabs()">
      <div class="page-header-inner">
        @if (eyebrow()) {
          <span class="eyebrow">{{ eyebrow() }}</span>
        }
        <h1 class="page-title">
          @if (icon(); as ic) {
            <i [class]="'pi ' + ic" class="page-title-icon" aria-hidden="true"></i>
          }
          <span>{{ title() }}</span>
        </h1>
        @if (subtitle()) {
          <p class="page-subtitle">{{ subtitle() }}</p>
        }
      </div>
      <div class="page-header-actions">
        <ng-content select="[slot=actions]" />
      </div>
    </header>
    <div class="page-header-guide">
      <ng-content select="[slot=guide]" />
    </div>
  `,
})
export class PageHeaderComponent {
  readonly title = input.required<string>();
  readonly subtitle = input<string | undefined>(undefined);
  readonly eyebrow = input<string | undefined>(undefined);
  /** Optional leading PrimeIcons class (without the ``pi-`` prefix —
   *  e.g. ``"pi-bolt"``). Renders in ``--accent`` next to the title. */
  readonly icon = input<string | undefined>(undefined);
  /** Set to true when a tab strip is rendered immediately below this
   *  header — drops the bottom margin so the two read as one SubNav
   *  unit per the design hand-off. */
  readonly hasTabs = input(false);
}
