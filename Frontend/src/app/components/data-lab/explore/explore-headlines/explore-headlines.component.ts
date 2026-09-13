import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { NewsService } from '../../../../services/news.service';
import type { NewsArticle } from '../../../../services/news.service';

import { DataLabWorkspaceStore } from '../../data-lab-workspace-store';
import { windowToNewsQuery, NEWS_MAX_HEADLINES } from '../news-window-adapter';

/** Local news-section state beyond the store's coarse newsState. */
type NewsSectionState = 'idle' | 'loading' | 'ready' | 'stale' | 'error' | 'rate-limited';

/**
 * Collapsed headlines section of Data Lab Explore (PRD §11).
 *
 * Lazy by design: no request exists until the section is expanded
 * (FR-010), and a window change invalidates loaded headlines — the
 * section turns stale instead of silently refetching. Responses that
 * arrive after the ticker/window changed are dropped.
 */
@Component({
  selector: 'app-explore-headlines',
  imports: [RouterLink, TimestampDisplayComponent],
  templateUrl: './explore-headlines.component.html',
  styleUrl: './explore-headlines.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExploreHeadlinesComponent {
  readonly store = inject(DataLabWorkspaceStore);
  private readonly newsService = inject(NewsService);

  /** Committed ticker the headlines belong to (null before a commit). */
  readonly ticker = input<string | null>(null);
  /** Committed int64 ms UTC window the headlines belong to. */
  readonly window = input<{ startMsUtc: number; endMsUtc: number } | null>(null);

  readonly newsExpanded = signal(false);
  readonly newsState = signal<NewsSectionState>('idle');
  readonly newsArticles = signal<readonly NewsArticle[]>([]);
  readonly newsError = signal('');

  readonly newsHeadlines = computed(() => this.newsArticles().slice(0, NEWS_MAX_HEADLINES));

  readonly hasCommittedScope = computed(() => !!this.ticker() && !!this.window());

  constructor() {
    // A committed-window change invalidates already-loaded headlines —
    // mark the section stale instead of silently refetching (PRD §11).
    effect(
      () => {
        this.window();
        untracked(() => {
          if (this.newsArticles().length > 0 && this.newsState() === 'ready') {
            this.newsState.set('stale');
            this.store.setNewsState('stale');
          }
        });
      },
      { allowSignalWrites: true },
    );
  }

  toggleNews(): void {
    this.newsExpanded.update((v) => !v);
    // Lazy: fetch only when expanded (PRD §11 / FR-010).
    if (this.newsExpanded() && this.newsState() === 'idle') this.fetchNews();
  }

  refreshNews(): void {
    this.fetchNews();
  }

  private async fetchNews(): Promise<void> {
    const ticker = this.ticker();
    const window = this.window();
    if (!ticker || !window) {
      this.newsState.set('idle');
      return;
    }
    this.newsState.set('loading');
    this.store.setNewsState('loading');
    this.newsError.set('');
    try {
      const result = await firstValueFrom(
        this.newsService.news(windowToNewsQuery(ticker, window)),
      );
      // The committed scope may have changed while the request was pending —
      // a late response for the old scope must not overwrite the section.
      if (this.isStaleNewsScope(ticker, window)) return;
      this.newsArticles.set(result.articles);
      this.newsState.set('ready');
      this.store.setNewsState('ready');
    } catch (e: unknown) {
      if (this.isStaleNewsScope(ticker, window)) return;
      const status = (e as { status?: number }).status;
      if (status === 429) {
        this.newsState.set('rate-limited');
        this.store.setNewsState('rate-limited');
        this.newsError.set('News vendor rate limit reached. Try again shortly.');
      } else {
        this.newsState.set('error');
        this.store.setNewsState('error');
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
