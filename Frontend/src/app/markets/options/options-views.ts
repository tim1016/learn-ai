import { defineWorkspaceTabs } from '../../shared/workspace/workspace-tabs';

/** The views of an instrument's Options tab (`/markets/:symbol/options/<view>`), declared once. */
export type OptionsView = 'chain' | 'surface' | 'pricing';

export const OPTIONS_VIEWS = defineWorkspaceTabs<OptionsView>(
  { chain: 'Chain', surface: 'Surface', pricing: 'Pricing' },
  ['chain', 'surface', 'pricing'],
);

/** The view `/markets/:symbol/options` opens on. */
export const DEFAULT_OPTIONS_VIEW: OptionsView = 'chain';
