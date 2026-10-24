import { defineWorkspaceTabs } from '../shared/workspace/workspace-tabs';

/**
 * The Options setups place's URL vocabulary (ADR 0084).
 *
 * A saved name addresses one setup, always with its tab, so a bare second
 * segment never names one. Evidence is addressed by what it froze (a run id,
 * a study id, a Golden Options id) and needs no name at all.
 */

export const OPTIONS_SETUPS_ROOT = '/options-setups';

/**
 * Where a draft with no saved name lives. A saved name starts with a letter
 * or a digit, so this can never be one: a new setup, an imported file and the
 * setup of a past run all open here until they are saved under a name.
 */
export const UNSAVED_DRAFT_SEGMENT = '_new';

export type OptionsSetupTab = 'overview' | 'define' | 'explore' | 'golden';

/** The tabs, declared once: the strip and the window title read the same names. */
export const OPTIONS_SETUP_TABS = defineWorkspaceTabs<OptionsSetupTab>(
  { overview: 'Overview', define: 'Define', explore: 'Explore', golden: 'Golden' },
  ['overview', 'define', 'explore', 'golden'],
);

/**
 * The tabs this place cannot open yet, and why. They stay in the strip so the
 * workspace shows what it will have, and are not routes until they exist.
 */
export const OPTIONS_SETUP_TAB_UNAVAILABLE: Readonly<Partial<Record<OptionsSetupTab, string>>> = {
  golden: 'Approving a tested run and starting a golden search are being moved into this place. Approved versions open from the Options setups list.',
};

/**
 * What Explore shows, by the `view` of its address (`/options-setups/:name/explore?view=`). Backtest is the one
 * that opens when the address names none. Performance, Research lineage and Historical comparison are about one run,
 * the `run` of the address.
 */
export type OptionsExploreView = 'backtest' | 'history' | 'chain' | 'performance' | 'lineage' | 'comparison';

export const OPTIONS_EXPLORE_VIEWS = defineWorkspaceTabs<OptionsExploreView>(
  { backtest: 'Backtest', history: 'History', chain: 'Past chain', performance: 'Performance', lineage: 'Research lineage', comparison: 'Historical comparison' },
  ['backtest', 'history', 'chain', 'performance', 'lineage', 'comparison'],
);

export const OPTIONS_EXPLORE_DEFAULT_VIEW: OptionsExploreView = 'backtest';

const EXPLORE_VIEWS: ReadonlySet<string> = new Set(OPTIONS_EXPLORE_VIEWS.tabs.map((tab) => tab.id));

export function isOptionsExploreView(value: string): value is OptionsExploreView {
  return EXPLORE_VIEWS.has(value);
}

/** The views that are about one run, and so need the address to name it. */
export function isRunView(view: OptionsExploreView): boolean {
  return view === 'performance' || view === 'lineage' || view === 'comparison';
}

/** The query of an Explore address. A view the address would open anyway, and a run it does not name, leave no key. */
export function optionsExploreQuery(view: OptionsExploreView, run: number | null = null, comparator: number | null = null): Record<string, string | null> {
  return {
    view: view === OPTIONS_EXPLORE_DEFAULT_VIEW ? null : view,
    run: run === null ? null : String(run),
    comparator_run_id: comparator === null ? null : String(comparator),
  };
}

const TABS: ReadonlyMap<string, OptionsSetupTab> = new Map(
  OPTIONS_SETUP_TABS.tabs.map((tab): [string, OptionsSetupTab] => [tab.id, tab.id]),
);

export type OptionsEvidenceKind = 'run' | 'study' | 'golden';

const EVIDENCE_KINDS: ReadonlySet<string> = new Set<OptionsEvidenceKind>(['run', 'study', 'golden']);

export function isOptionsEvidenceKind(value: string): value is OptionsEvidenceKind {
  return EVIDENCE_KINDS.has(value);
}

/** One setup's tab. `null` is the unsaved draft. */
export function optionsSetupRoute(name: string | null, tab: OptionsSetupTab): string[] {
  return [OPTIONS_SETUPS_ROOT, name ?? UNSAVED_DRAFT_SEGMENT, tab];
}

/** Immutable evidence, by what it froze. It needs no saved name. */
export function optionsEvidenceRoute(kind: OptionsEvidenceKind, id: string | number): string[] {
  return [OPTIONS_SETUPS_ROOT, 'evidence', kind, String(id)];
}

export interface OptionsSetupLocation {
  /** The saved name, or `null` for the unsaved draft. */
  readonly name: string | null;
  readonly tab: OptionsSetupTab;
}

/** The setup and tab a URL names, or `null` for any other address (the index, evidence, a lost address). */
export function optionsSetupLocation(url: string): OptionsSetupLocation | null {
  const path = url.split(/[?#]/)[0];
  const segments = path.split('/').filter((part) => part !== '');
  if (segments.length !== 3 || `/${segments[0]}` !== OPTIONS_SETUPS_ROOT) return null;
  const tab = TABS.get(segments[2]);
  if (tab === undefined) return null;
  const name = safeDecode(segments[1]);
  return { name: name === UNSAVED_DRAFT_SEGMENT ? null : name, tab };
}

function safeDecode(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/** The window title of a setup's tab: `SPY 0DTE momentum call · Define · Options setups`. */
export function optionsSetupWindowTitle(url: string): string | null {
  const location = optionsSetupLocation(url);
  if (location === null) return null;
  return `${location.name ?? 'New setup'} · ${OPTIONS_SETUP_TABS.label(location.tab)} · Options setups`;
}
