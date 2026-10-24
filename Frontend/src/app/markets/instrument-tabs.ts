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
  returns: 'Not routed here yet. Return Distribution in the Markets menu still runs the study.',
  data: 'Not routed here yet. The Data Lake Observatory under Operations shows what the lake holds.',
  options: 'Not routed here yet. Options Lab in the Markets menu still shows the chain.',
};
