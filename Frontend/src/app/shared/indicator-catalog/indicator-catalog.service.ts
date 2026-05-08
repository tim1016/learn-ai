import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../environments/environment';

export interface IndicatorParamConfig {
  name: string;
  type: 'int' | 'float';
  default: number;
  min: number;
  max: number;
  description: string;
}

export interface IndicatorInfo {
  name: string;
  category: string;
  description: string;
  configurable_params: IndicatorParamConfig[];
}

export interface IndicatorCategory {
  name: string;
  indicators: IndicatorInfo[];
}

interface AvailableResponse {
  success: boolean;
  categories: Record<string, IndicatorInfo[]>;
  total: number;
}

/**
 * Loads the pandas-ta indicator catalog from `/api/dataset/available` once
 * per app session and caches it. Both data-lab and the research-lab
 * feature/signal runners consume the same source so the visible indicator
 * set stays in lockstep.
 */
@Injectable({ providedIn: 'root' })
export class IndicatorCatalogService {
  private http = inject(HttpClient);

  private readonly _categories = signal<IndicatorCategory[]>([]);
  private readonly _indicatorMap = signal<Record<string, IndicatorInfo>>({});
  private readonly _loading = signal<boolean>(false);
  private readonly _error = signal<string | null>(null);

  readonly categories = this._categories.asReadonly();
  readonly indicatorMap = this._indicatorMap.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  readonly loaded = computed<boolean>(() => this._categories().length > 0);

  private inflight: Promise<void> | null = null;

  /** Load the catalog. Idempotent — concurrent calls share one HTTP request,
   *  and once loaded the cached data is reused. */
  async load(): Promise<void> {
    if (this.loaded() || this._loading()) {
      if (this.inflight) await this.inflight;
      return;
    }
    this._loading.set(true);
    this._error.set(null);
    this.inflight = (async () => {
      try {
        const response = await firstValueFrom(
          this.http.get<AvailableResponse>(
            `${environment.pythonServiceUrl}/api/dataset/available`,
          ),
        );
        if (!response.success) {
          this._error.set('Failed to load indicators');
          return;
        }
        const catList: IndicatorCategory[] = [];
        const map: Record<string, IndicatorInfo> = {};
        for (const [catName, items] of Object.entries(response.categories)) {
          catList.push({ name: catName, indicators: items });
          for (const item of items) map[item.name] = item;
        }
        this._categories.set(catList);
        this._indicatorMap.set(map);
      } catch (e: unknown) {
        this._error.set(e instanceof Error ? e.message : String(e));
      } finally {
        this._loading.set(false);
        this.inflight = null;
      }
    })();
    await this.inflight;
  }

  /** Look up an indicator's metadata (params, description, category). */
  get(name: string): IndicatorInfo | null {
    return this._indicatorMap()[name] ?? null;
  }

  /** Build a default param map for the given indicator, using the catalog's
   *  declared defaults. Useful when seeding a new selection. */
  defaultParams(name: string): Record<string, number> {
    const info = this.get(name);
    if (!info) return {};
    const out: Record<string, number> = {};
    for (const p of info.configurable_params) out[p.name] = p.default;
    return out;
  }
}
