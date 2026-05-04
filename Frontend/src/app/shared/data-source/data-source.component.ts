import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

export type DataSourceOrigin = 'IBKR' | 'Polygon' | 'Engine' | 'App' | string;

/**
 * Provenance caption for any chart, table, or numeric block.
 *
 * Single grey one-liner that names the upstream source, the method
 * used to derive the displayed values, and a freshness stamp. When
 * ``delayedFallback`` is true the caption flips amber and reads
 * ``IBKR · 15-min delayed (no OPRA subscription)`` — this is the
 * lever for the silent-degradation failure mode where a paper account
 * without an OPRA subscription falls back to 15-min delayed quotes
 * without anything visibly changing on screen.
 */
@Component({
  selector: 'app-data-source',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './data-source.component.scss',
  template: `
    <p
      class="data-source"
      [class.data-source--delayed]="delayedFallback()"
      [attr.role]="delayedFallback() ? 'status' : null"
      [attr.aria-live]="delayedFallback() ? 'polite' : null"
    >
      <span class="data-source-origin">{{ origin() }}</span>
      <span class="data-source-sep" aria-hidden="true">·</span>
      <span class="data-source-text">{{ caption() }}</span>
    </p>
  `,
})
export class DataSourceComponent {
  readonly origin = input.required<DataSourceOrigin>();
  readonly method = input<string | undefined>(undefined);
  readonly freshness = input<string | undefined>(undefined);
  readonly delayedFallback = input(false);

  readonly caption = computed(() => {
    if (this.delayedFallback()) {
      return '15-min delayed (no OPRA subscription)';
    }
    const parts: string[] = [];
    const m = this.method();
    const f = this.freshness();
    if (m) parts.push(m);
    if (f) parts.push(f);
    return parts.join(' · ');
  });
}
