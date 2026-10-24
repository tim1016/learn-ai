import type { components } from '../../api/broker.types';
import { operationsTabLabel, operationsTabRoute } from '../../operations/operations-tabs';

/**
 * The scoped-health projection, as the data plane sends it (PRD #3490 R30).
 *
 * The frontend has no healthy-or-stale rule of its own: severity, freshness,
 * which operations a finding affects and whether a page should say so
 * (`needs_attention`) all arrive in the items. What lives here is wording.
 */
export type ScopedHealth = components['schemas']['ScopedHealthResponse'];
export type ScopedHealthItem = components['schemas']['HealthItem'];
export type HealthSource = ScopedHealthItem['source'];
export type HealthSeverity = ScopedHealthItem['severity'];
export type AffectedOperation = ScopedHealthItem['affects'][number];

/** The chips, in the order they are shown. Typed so a source the data plane adds fails to compile until it is named here. */
export const HEALTH_SOURCES = ['lake_catalog', 'option_archive', 'backups', 'ibkr_feed'] as const satisfies readonly HealthSource[];

export const SOURCE_LABEL: Readonly<Record<HealthSource, string>> = {
  lake_catalog: 'Lake',
  option_archive: 'Option archive',
  backups: 'Backups',
  ibkr_feed: 'IBKR feed',
};

export const SEVERITY_WORD: Readonly<Record<HealthSeverity, string>> = {
  critical: 'Failing',
  unknown: 'Unknown',
  warning: 'Needs attention',
  info: 'Notice',
  ok: 'OK',
};

/**
 * What each kind of page does, in the projection's terms: the operations whose
 * failure it should hear about. A page names what it does; the data plane
 * decides what that is affected by.
 */
export const HEALTH_NEEDS = {
  /** Runs on archived option history, whose backups decide whether that history can be recovered. */
  archivedOptions: ['options_history', 'recovery'],
  /** The live option chain and surface, streamed from the IBKR feed. */
  liveChain: ['live_chain'],
  /**
   * Studies, charts and exports that read a symbol's stock bars from the lake.
   * The data plane speaks for the split-adjusted trade bars over the symbol's whole held
   * span, not for one date window or another price-adjustment view: a page that reads
   * a different view shows no notice, and the Lake tab's filters show the rest.
   */
  stockHistory: ['stock_history'],
} as const satisfies Readonly<Record<string, readonly AffectedOperation[]>>;

/** Where a finding's evidence is shown: the router's commands for an Operations tab, with the filters and card that show it. */
export interface OperationsLink {
  readonly label: string;
  readonly route: readonly string[];
  readonly query: Readonly<Record<string, string>>;
  readonly fragment: string | undefined;
}

/** The link a chip or a notice offers for `item`, or `null` where no Operations tab shows its evidence. */
export function operationsLinkOf(item: ScopedHealthItem): OperationsLink | null {
  const { link } = item;
  if (link === null || link === undefined) return null;
  return {
    label: operationsTabLabel(link.tab),
    route: operationsTabRoute(link.tab),
    query: link.query ?? {},
    fragment: link.fragment ?? undefined,
  };
}
