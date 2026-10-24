import { defineWorkspaceTabs } from '../../shared/workspace/workspace-tabs';

/** The views of an instrument's Options tab that are routed (`/markets/:symbol/options/<view>`). */
export type OptionsView = 'surface' | 'pricing';

export const OPTIONS_VIEWS = defineWorkspaceTabs<OptionsView>(
  { surface: 'Surface', pricing: 'Pricing' },
  ['surface', 'pricing'],
);

/** The view `/markets/:symbol/options` opens on until the chain view is routed. */
export const DEFAULT_OPTIONS_VIEW: OptionsView = 'surface';
