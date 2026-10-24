import { ChangeDetectionStrategy, Component, computed, inject, linkedSignal } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';

import { SectionErrorComponent } from '../../shared/errors/section-error.component';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { NewsService, type NewsQuery, type NewsResult } from '../../services/news.service';
import { windowToNewsQuery } from '../chart/news-window-adapter';
import { instrumentCommands } from '../instrument-address';
import { windowMsUtc } from '../instrument-context';
import { InstrumentContextService } from '../instrument-context.service';
import { NewsArticleCardComponent } from './news-article-card.component';
import { NewsQueryFormComponent } from './news-query-form.component';

/**
 * Every headline for one instrument (ADR 0084: `/markets/:symbol/chart/news`),
 * newest first, for the window the Chart tab's headlines were read for. The
 * instrument is the address's, never a default: the query is for that symbol,
 * and moving to another instrument or window starts over from its own opening
 * query.
 */
@Component({
  selector: 'app-news-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    NewsArticleCardComponent,
    NewsQueryFormComponent,
    SectionErrorComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './news-page.component.html',
  styleUrl: './news-page.component.scss',
})
export class NewsPageComponent {
  private readonly news = inject(NewsService);

  private readonly context = inject(InstrumentContextService);

  /** The instrument whose headlines these are, from the address. */
  protected readonly ticker = this.context.symbol;

  /** Back to the chart, with the context the address carries. */
  protected readonly chartCommands = computed(() => instrumentCommands(this.ticker(), 'chart'));

  /** Opening query: this instrument over the committed window, newest first. */
  readonly defaultQuery = computed<NewsQuery>(() => {
    const committed = this.context.context();
    const window = committed === null ? null : windowMsUtc(committed.window);
    return window === null
      ? { ticker: this.ticker(), sort: 'published_utc', order: 'desc', limit: 50 }
      : windowToNewsQuery(this.ticker(), window, 50);
  });

  /**
   * What has actually been asked for. The form holds the draft; this only
   * moves on submit, because the upstream plan allows 5 requests/minute and
   * fetching per keystroke would burn the budget in seconds. A different
   * instrument starts over from its own opening query.
   */
  private readonly submitted = linkedSignal<NewsQuery>(() => ({ ...this.defaultQuery() }));

  private readonly resource = rxResource<NewsResult, NewsQuery>({
    params: () => this.submitted(),
    stream: ({ params }) => this.news.news(params),
  });

  readonly isLoading = this.resource.isLoading;
  readonly error = this.resource.error;

  // `value()` throws while the resource is in an error state, and the header
  // reads this regardless of outcome — so gate on `hasValue()`, never `??`.
  readonly result = computed(() => (this.resource.hasValue() ? this.resource.value() : null));
  readonly articles = computed(() => this.result()?.articles ?? []);
  readonly hasArticles = computed(() => this.articles().length > 0);

  /** Ticker whose sentiment is surfaced first on each article. */
  protected readonly focusTicker = computed(() => this.submitted().ticker?.trim().toUpperCase() || null);

  onSubmitted(query: NewsQuery): void {
    this.submitted.set(query);
  }

  onCleared(): void {
    this.submitted.set({ ...this.defaultQuery() });
  }

  reload(): void {
    this.resource.reload();
  }

  /** Vendor ids can be null; fall back to the URL, then position. */
  articleKey(articleId: string | null | undefined, articleUrl: string | null | undefined, index: number): string {
    return articleId ?? articleUrl ?? `article-${index}`;
  }
}
