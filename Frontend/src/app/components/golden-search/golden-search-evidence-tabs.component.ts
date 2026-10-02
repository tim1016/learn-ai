import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, input, resource, signal } from '@angular/core';

import { extractServerMessage } from '../broker/operation-error';
import type { CandidateRow } from './golden-search-compare';
import { GoldenSearchMonthlyComponent } from './golden-search-monthly.component';
import { GoldenSearchNeighborhoodComponent } from './golden-search-neighborhood.component';
import { GoldenSearchPairMapComponent } from './golden-search-pair-map.component';
import { GoldenSearchReturnLinesComponent, type ReturnLineSeries } from './golden-search-return-lines.component';
import { GoldenSearchTradesComponent } from './golden-search-trades.component';
import { GoldenSearchService } from './golden-search.service';
import type { CandidateDetail, CandidateKey, PairMap, Point, StrategyCapability, StudyScope } from './golden-search.types';

export type EvidenceTab = 'map' | 'equity' | 'months' | 'trades' | 'neighbors' | 'stress';

export const EVIDENCE_TABS: readonly { id: EvidenceTab; label: string }[] = [
  { id: 'map', label: 'Parameter map' },
  { id: 'equity', label: 'Equity' },
  { id: 'months', label: 'By month' },
  { id: 'trades', label: 'Trades' },
  { id: 'neighbors', label: 'Neighborhood' },
  { id: 'stress', label: 'Stress' },
];

/** Tabs that read the candidates' detail runs; they load the first time one opens. */
const DETAIL_TABS: ReadonlySet<EvidenceTab> = new Set(['equity', 'months', 'trades']);

interface DetailRequest {
  readonly studyId: string;
  readonly keys: readonly CandidateKey[];
}

/**
 * The Compare step's evidence (#2696): the parameter map, every candidate's
 * development equity, the selected candidate's months and trades, and its
 * neighborhood and stress runs. The detail runs are read only once a tab
 * needs them. Everything shown is development evidence, used to choose.
 */
@Component({
  selector: 'app-golden-search-evidence-tabs',
  imports: [DecimalPipe, GoldenSearchMonthlyComponent, GoldenSearchNeighborhoodComponent, GoldenSearchPairMapComponent, GoldenSearchReturnLinesComponent, GoldenSearchTradesComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-evidence-tabs.component.html',
  styleUrl: './golden-search-evidence-tabs.component.scss',
})
export class GoldenSearchEvidenceTabsComponent {
  private readonly service = inject(GoldenSearchService);

  readonly studyId = input.required<string>();
  readonly rows = input.required<readonly CandidateRow[]>();
  readonly selected = input.required<CandidateRow>();
  readonly pairMaps = input.required<readonly PairMap[]>();
  /** The settings the pair maps are drawn through (the all-period fit). */
  readonly mapCenter = input<Point | null>(null);
  readonly mapCenterLabel = input.required<string>();
  readonly capability = input<StrategyCapability | null>(null);
  readonly scope = input.required<StudyScope>();

  protected readonly tabs = EVIDENCE_TABS;
  readonly active = signal<EvidenceTab>('map');
  private readonly detailWanted = signal(false);

  private readonly detailRequest = computed<DetailRequest | undefined>(
    () => (this.detailWanted() ? { studyId: this.studyId(), keys: this.rows().map((row) => row.key) } : undefined),
    { equal: (a, b) => a?.studyId === b?.studyId && a?.keys.join() === b?.keys.join() },
  );

  protected readonly details = resource({
    params: () => this.detailRequest(),
    loader: async ({ params }) => {
      const entries = await Promise.all(params.keys.map(async (key) => [key, await this.service.candidate(params.studyId, key)] as const));
      return new Map<CandidateKey, CandidateDetail>(entries);
    },
  });

  protected readonly detailsError = computed(() => {
    const error = this.details.error();
    return error === undefined ? null : extractServerMessage(error, 'The candidates’ detail runs could not be loaded.');
  });

  protected readonly selectedRun = computed(() => (this.details.hasValue() ? (this.details.value().get(this.selected().key)?.development ?? null) : null));

  protected readonly equitySeries = computed<ReturnLineSeries[]>(() => {
    if (!this.details.hasValue()) return [];
    const details = this.details.value();
    const selected = this.selected().key;
    return this.rows().map((row) => ({
      key: row.key,
      label: row.candidate.label,
      tone: row.key === selected ? 'primary' : row.key === 'incumbent' ? 'benchmark' : 'secondary',
      points: details.get(row.key)?.development?.cumulative_return ?? [],
    }));
  });

  open(tab: EvidenceTab): void {
    this.active.set(tab);
    if (DETAIL_TABS.has(tab)) this.detailWanted.set(true);
  }

  protected retry(): void {
    this.details.reload();
  }
}
