import { defineWorkspaceTabs } from '../shared/workspace/workspace-tabs';

/** The tabs of an instrument's place (ADR 0084), named once. */
export type InstrumentTab = 'chart' | 'returns' | 'data' | 'options';

export const INSTRUMENT_TABS = defineWorkspaceTabs<InstrumentTab>(
  { chart: 'Chart', returns: 'Returns', data: 'Data', options: 'Options' },
  ['chart', 'returns', 'data', 'options'],
);
