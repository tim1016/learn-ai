import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { map, type Observable } from 'rxjs';

import type { components } from '../api/broker.types';
import { environment } from '../../environments/environment';

/**
 * Ticker news from `/api/news`, which passes every Polygon
 * `/v2/reference/news` query parameter straight through.
 *
 * Sentiment on an article is **vendor-asserted**: Polygon produces it with an
 * unpublished model we cannot reimplement, so it is recorded, never validated,
 * and never comparable to a derived feature from the feature registry. The
 * response carries `sentiment_provenance` so the UI can say so out loud.
 *
 * The wire types come straight from the generated OpenAPI contract rather than
 * being hand-copied, so a field added on the Python side cannot silently go
 * missing here.
 */
export type NewsPublisher = components['schemas']['NewsPublisher'];
export type NewsInsight = components['schemas']['NewsInsight'];
export type NewsArticle = components['schemas']['NewsArticle'];
export type NewsResult = components['schemas']['NewsResponse'];

/**
 * Every parameter the endpoint accepts. Field names are the wire names: these
 * are vendor query parameters, not domain concepts, so there is nothing to
 * translate and no rename map to drift.
 */
export interface NewsQuery {
  ticker?: string;
  ticker_gte?: string;
  ticker_gt?: string;
  ticker_lte?: string;
  ticker_lt?: string;
  published_utc?: string;
  published_utc_gte?: string;
  published_utc_gt?: string;
  published_utc_lte?: string;
  published_utc_lt?: string;
  sort?: string;
  order?: 'asc' | 'desc';
  limit?: number;
}

function newsParams(query: NewsQuery): HttpParams {
  let params = new HttpParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue;
    params = params.set(key, String(value));
  }
  return params;
}

@Injectable({ providedIn: 'root' })
export class NewsService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/news`;

  news(query: NewsQuery): Observable<NewsResult> {
    return this.http
      .get<NewsResult>(this.base, { params: newsParams(query) })
      .pipe(map((response) => ({ ...response, articles: response.articles ?? [] })));
  }
}
