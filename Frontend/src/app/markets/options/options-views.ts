import { defineWorkspaceTabs } from '../../shared/workspace/workspace-tabs';

/** The views of an instrument's Options tab (`/markets/:symbol/options/<view>`), declared once. */
export type OptionsView = 'chain' | 'surface' | 'pricing';

export const OPTIONS_VIEWS = defineWorkspaceTabs<OptionsView>(
  { chain: 'Chain', surface: 'Surface', pricing: 'Pricing' },
  ['chain', 'surface', 'pricing'],
);

/** The view `/markets/:symbol/options` opens on. */
export const DEFAULT_OPTIONS_VIEW: OptionsView = 'chain';

/**
 * The views this tab does not route yet, each with the reason its entry shows greyed. A view
 * leaves this list when its page is routed under `/markets/:symbol/options/<view>`; the strip,
 * the labels and the redirects need no other edit.
 */
export const UNAVAILABLE_OPTIONS_VIEWS: Readonly<Partial<Record<OptionsView, string>>> = {
  surface: 'Not in this place yet. Options Surface (3D), in the Markets menu, still shows it.',
  pricing: 'Not in this place yet. Pricing Lab, in the Markets menu, still prices a contract.',
};
