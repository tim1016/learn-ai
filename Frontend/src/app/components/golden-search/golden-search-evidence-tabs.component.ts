import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';

import type { CandidateRow } from './golden-search-compare';
import { GoldenSearchMonthlyComponent } from './golden-search-monthly.component';
import { GoldenSearchPairMapComponent } from './golden-search-pair-map.component';
import { GoldenSearchTradesComponent } from './golden-search-trades.component';
import type { CandidateDetail, CandidateKey, PairMap, Point, StrategyCapability } from './golden-search.types';

export type EvidenceTab = 'map' | 'months' | 'trades';

export const EVIDENCE_TABS: readonly { id: EvidenceTab; label: string }[] = [
  { id: 'map', label: 'Parameter map' },
  { id: 'months', label: 'By month' },
  { id: 'trades', label: 'Trades' },
];

/**
 * The Compare step's evidence (#2696): the parameter map and the selected
 * candidate's months and trades. The detail runs come from the step, which
 * reads them for its charts. Everything shown is development evidence, used
 * to choose.
 */
@Component({
  selector: 'app-golden-search-evidence-tabs',
  imports: [GoldenSearchMonthlyComponent, GoldenSearchPairMapComponent, GoldenSearchTradesComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-evidence-tabs.component.html',
  styleUrl: './golden-search-evidence-tabs.component.scss',
})
export class GoldenSearchEvidenceTabsComponent {
  readonly selected = input.required<CandidateRow>();
  /** The candidates' detail runs; null while they load. */
  readonly details = input.required<ReadonlyMap<CandidateKey, CandidateDetail> | null>();
  readonly detailsError = input<string | null>(null);
  readonly pairMaps = input.required<readonly PairMap[]>();
  /** The settings the pair maps are drawn through (the all-period fit). */
  readonly mapCenter = input<Point | null>(null);
  readonly mapCenterLabel = input.required<string>();
  readonly capability = input<StrategyCapability | null>(null);
  readonly retry = output();

  protected readonly tabs = EVIDENCE_TABS;
  readonly active = signal<EvidenceTab>('map');

  protected readonly selectedRun = computed(() => this.details()?.get(this.selected().key)?.development ?? null);

  open(tab: EvidenceTab): void {
    this.active.set(tab);
  }
}
