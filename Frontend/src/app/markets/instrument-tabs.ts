import { defineWorkspaceTabs } from '../shared/workspace/workspace-tabs';

/** The tabs of an instrument's place (ADR 0084), named once. */
export type InstrumentTab = 'chart' | 'returns' | 'data' | 'options';

export const INSTRUMENT_TABS = defineWorkspaceTabs<InstrumentTab>(
  { chart: 'Chart', returns: 'Returns', data: 'Data', options: 'Options' },
  ['chart', 'returns', 'data', 'options'],
);

/**
 * The tabs this place does not route yet, each with the reason its tab shows
 * greyed. A tab leaves this list when its page is routed under
 * `/markets/:symbol/<tab>`; the strip, the labels and the window title need
 * no other edit.
 */
export const UNAVAILABLE_INSTRUMENT_TABS: Readonly<Partial<Record<InstrumentTab, string>>> = {
  options: 'Not in this place yet. Options Lab, in the Markets menu, still shows the chain.',
};
