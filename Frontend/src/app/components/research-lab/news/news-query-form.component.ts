import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, output } from '@angular/core';

import type { NewsQuery } from '../../../services/news.service';

/** Text fields on the form. Keys are the wire parameter names. */
export type NewsQueryField = Exclude<keyof NewsQuery, 'limit' | 'order'>;

interface FieldSpec {
  readonly field: NewsQueryField;
  readonly label: string;
  readonly placeholder: string;
}

const TICKER_FIELDS: readonly FieldSpec[] = [
  { field: 'ticker', label: 'Ticker', placeholder: 'SPY' },
  { field: 'ticker_gte', label: 'Ticker ≥', placeholder: 'A' },
  { field: 'ticker_gt', label: 'Ticker >', placeholder: 'A' },
  { field: 'ticker_lte', label: 'Ticker ≤', placeholder: 'Z' },
  { field: 'ticker_lt', label: 'Ticker <', placeholder: 'Z' },
];

const DATE_FIELDS: readonly FieldSpec[] = [
  { field: 'published_utc', label: 'Published on', placeholder: 'YYYY-MM-DD' },
  { field: 'published_utc_gte', label: 'Published ≥', placeholder: 'YYYY-MM-DD' },
  { field: 'published_utc_gt', label: 'Published >', placeholder: 'YYYY-MM-DD' },
  { field: 'published_utc_lte', label: 'Published ≤', placeholder: 'YYYY-MM-DD' },
  { field: 'published_utc_lt', label: 'Published <', placeholder: 'YYYY-MM-DD' },
];

/**
 * The query editor. Holds the *draft* only — it emits on submit rather than on
 * every keystroke because the upstream plan allows 5 requests/minute.
 */
@Component({
  selector: 'app-news-query-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './news-query-form.component.html',
  styleUrl: './news-query-form.component.scss',
})
export class NewsQueryFormComponent {
  readonly initial = input.required<NewsQuery>();
  readonly busy = input(false);

  readonly submitted = output<NewsQuery>();
  readonly cleared = output();

  readonly tickerFields = TICKER_FIELDS;
  readonly dateFields = DATE_FIELDS;

  /** Local edits, reset whenever the page hands down a new starting query. */
  private readonly draft = linkedSignal<NewsQuery>(() => ({ ...this.initial() }));

  readonly limit = computed(() => this.draft().limit ?? 50);
  readonly order = computed(() => this.draft().order ?? 'desc');
  readonly sort = computed(() => this.draft().sort ?? '');

  valueOf(field: NewsQueryField): string {
    return this.draft()[field] ?? '';
  }

  /** Narrows an input event to its string value, keeping `$any` out of the template. */
  inputValue(event: Event): string {
    const target = event.target;
    return target instanceof HTMLInputElement || target instanceof HTMLSelectElement ? target.value : '';
  }

  setField(field: NewsQueryField, value: string): void {
    this.draft.update((q) => ({ ...q, [field]: value }));
  }

  setSort(value: string): void {
    this.draft.update((q) => ({ ...q, sort: value }));
  }

  setOrder(value: string): void {
    this.draft.update((q) => ({ ...q, order: value === 'asc' ? 'asc' : 'desc' }));
  }

  setLimit(value: string): void {
    const parsed = Number.parseInt(value, 10);
    if (Number.isNaN(parsed)) return;
    this.draft.update((q) => ({ ...q, limit: Math.min(1000, Math.max(1, parsed)) }));
  }

  submit(): void {
    this.submitted.emit({ ...this.draft() });
  }

  reset(): void {
    this.draft.set({ ...this.initial() });
    this.cleared.emit();
  }
}
