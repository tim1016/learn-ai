import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  input,
  signal,
  untracked,
  inject,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { NewsService } from '../../../services/news.service';
import type { NewsArticle } from '../../../services/news.service';
import { instrumentHeadlinesCommands } from '../../instrument-address';
import { windowToNewsQuery, NEWS_MAX_HEADLINES } from '../news-window-adapter';

/** Local news-section state. */
type NewsSectionState = 'idle' | 'loading' | 'ready' | 'stale' | 'error' | 'rate-limited';

/** Who the loaded headlines came from, and when they were read. */
interface NewsCredit {
  /** The news source's name as the response gave it; `null` when it named none. */
  readonly vendor: string | null;
  readonly fetchedAtMs: number;
}

/**
 * Collapsed headlines section of an instrument's Chart tab.
 *
 * Lazy by design: no request exists until the section is expanded, and a
 * window change invalidates loaded headlines — the section turns stale instead
 * of silently refetching, because the news source allows few requests a
 * minute. Responses that arrive after the ticker/window changed are dropped.
 *
 * News is read from its own source at the time it is asked for. It is credited
 * to that source and says it is not part of the chart's bars: the two are not
 * one snapshot (PRD #3490 R10).
 */
@Component({
  selector: 'app-chart-headlines',
  imports: [RouterLink, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './chart-headlines.component.html',
  styleUrl: './chart-headlines.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChartHeadlinesComponent {
  private readonly newsService = inject(NewsService);

  /** Committed ticker the headlines belong to (null before a commit). */
  readonly ticker = input<string | null>(null);
  /** Committed int64 ms UTC window the headlines belong to. */
  readonly window = input<{ startMsUtc: number; endMsUtc: number } | null>(null);

  readonly newsExpanded = signal(false);
  readonly newsState = signal<NewsSectionState>('idle');
  readonly newsArticles = signal<readonly NewsArticle[]>([]);
  readonly newsError = signal('');
  readonly credit = signal<NewsCredit | null>(null);

  readonly newsHeadlines = computed(() => this.newsArticles().slice(0, NEWS_MAX_HEADLINES));

  readonly hasCommittedScope = computed(() => !!this.ticker() && !!this.window());

  /** Every headline for the instrument, keeping the context in the address. */
  protected readonly allHeadlines = computed(() => instrumentHeadlinesCommands(this.ticker() ?? ''));

  /** The instrument and window the section speaks for, as one value: an equal commit is not a change. */
  private readonly contextKey = computed(() => {
    const window = this.window();
    return `${this.ticker() ?? ''}|${window?.startMsUtc ?? ''}|${window?.endMsUtc ?? ''}`;
  });

  constructor() {
    // A committed ticker or window change invalidates whatever the section
    // says, not only loaded headlines: "No headlines in this window", a
    // failure or a rate-limit all answered another context, and a request
    // still in flight is dropped when it lands. Mark the section stale
    // instead of silently refetching.
    effect(() => {
      this.contextKey();
      untracked(() => {
        if (this.newsState() !== 'idle') this.newsState.set('stale');
      });
    });
  }

  toggleNews(): void {
    this.newsExpanded.update((v) => !v);
    // Lazy: fetch only when expanded.
    if (this.newsExpanded() && this.newsState() === 'idle') void this.fetchNews();
  }

  refreshNews(): void {
    void this.fetchNews();
  }

  private async fetchNews(): Promise<void> {
    const ticker = this.ticker();
    const window = this.window();
    if (!ticker || !window) {
      this.newsState.set('idle');
      return;
    }
    this.newsState.set('loading');
    this.newsError.set('');
    try {
      const result = await firstValueFrom(
        this.newsService.news(windowToNewsQuery(ticker, window)),
      );
      // The committed scope may have changed while the request was pending —
      // a late response for the old scope must not overwrite the section.
      if (this.isStaleNewsScope(ticker, window)) return;
      this.newsArticles.set(result.articles);
      this.credit.set({ vendor: result.vendor ?? null, fetchedAtMs: result.fetched_at_ms });
      this.newsState.set('ready');
    } catch (e: unknown) {
      if (this.isStaleNewsScope(ticker, window)) return;
      const status = (e as { status?: number }).status;
      if (status === 429) {
        this.newsState.set('rate-limited');
        this.newsError.set('News vendor rate limit reached. Try again shortly.');
      } else {
        this.newsState.set('error');
        this.newsError.set(e instanceof Error ? e.message : String(e));
      }
    }
  }

  /** True when the scope a pending news request captured no longer matches
   *  the current committed scope (ticker or window changed mid-flight). */
  private isStaleNewsScope(ticker: string, window: { startMsUtc: number; endMsUtc: number }): boolean {
    const current = this.window();
    return (
      this.ticker() !== ticker ||
      !current ||
      current.startMsUtc !== window.startMsUtc ||
      current.endMsUtc !== window.endMsUtc
    );
  }
}
