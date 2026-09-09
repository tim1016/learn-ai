import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { NewsService, type NewsQuery, type NewsResult } from '../../../services/news.service';
import { NewsArticleCardComponent } from './news-article-card.component';
import { NewsQueryFormComponent } from './news-query-form.component';

/** Opening query — SPY, newest first. */
const DEFAULT_QUERY: NewsQuery = {
  ticker: 'SPY',
  sort: 'published_utc',
  order: 'desc',
  limit: 50,
};

@Component({
  selector: 'app-news-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
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

  readonly defaultQuery = DEFAULT_QUERY;

  /**
   * What has actually been asked for. The form holds the draft; this only
   * moves on submit, because the upstream plan allows 5 requests/minute and
   * fetching per keystroke would burn the budget in seconds.
   */
  private readonly submitted = signal<NewsQuery>({ ...DEFAULT_QUERY });

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

  /** Ticker whose icon heads the page — null when the query is a range. */
  readonly focusTicker = computed(() => this.submitted().ticker?.trim().toUpperCase() || null);

  onSubmitted(query: NewsQuery): void {
    this.submitted.set(query);
  }

  onCleared(): void {
    this.submitted.set({ ...DEFAULT_QUERY });
  }

  reload(): void {
    this.resource.reload();
  }

  /** Vendor ids can be null; fall back to the URL, then position. */
  articleKey(articleId: string | null | undefined, articleUrl: string | null | undefined, index: number): string {
    return articleId ?? articleUrl ?? `article-${index}`;
  }
}
