import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { NewsArticle, NewsInsight } from '../../../services/news.service';

/** Sentiment labels Polygon emits; anything else renders as unknown. */
export type SentimentTone = 'positive' | 'negative' | 'neutral' | 'unknown';

/** One insight plus the presentation facts the template needs. */
export interface InsightView {
  readonly key: string;
  readonly ticker: string;
  readonly sentiment: string;
  readonly reasoning: string | null;
  readonly isPositive: boolean;
  readonly isNegative: boolean;
  readonly isNeutral: boolean;
  readonly isUnknown: boolean;
}

function toneOf(sentiment: string | null | undefined): SentimentTone {
  switch (sentiment) {
    case 'positive':
    case 'negative':
    case 'neutral':
      return sentiment;
    default:
      return 'unknown';
  }
}

@Component({
  selector: 'app-news-article-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, TimestampDisplayComponent],
  templateUrl: './news-article-card.component.html',
  styleUrl: './news-article-card.component.scss',
})
export class NewsArticleCardComponent {
  readonly article = input.required<NewsArticle>();
  /** The ticker the page queried — its insight is surfaced first. */
  readonly focusTicker = input<string | null>(null);

  readonly publisherName = computed(() => this.article().publisher?.name ?? 'Unknown publisher');
  readonly tickers = computed(() => this.article().tickers ?? []);
  readonly keywords = computed(() => this.article().keywords ?? []);

  /**
   * Focus ticker's insight first, then the rest in vendor order.
   *
   * Tone is resolved to discrete booleans here rather than concatenating a
   * class name in the template, so a SCSS rename stays greppable.
   */
  readonly insights = computed<readonly InsightView[]>(() => {
    const focus = this.focusTicker()?.toUpperCase() ?? null;
    const raw = this.article().insights ?? [];
    const ordered = focus === null ? raw : [...raw].sort((a, b) => rank(a, focus) - rank(b, focus));
    return ordered.map((insight, index) => {
      const tone = toneOf(insight.sentiment);
      return {
        key: `${insight.ticker ?? '?'}:${index}`,
        ticker: insight.ticker ?? '—',
        sentiment: insight.sentiment ?? 'unknown',
        reasoning: insight.sentiment_reasoning ?? null,
        isPositive: tone === 'positive',
        isNegative: tone === 'negative',
        isNeutral: tone === 'neutral',
        isUnknown: tone === 'unknown',
      };
    });
  });
}

function rank(insight: NewsInsight, focus: string): number {
  return insight.ticker?.toUpperCase() === focus ? 0 : 1;
}
