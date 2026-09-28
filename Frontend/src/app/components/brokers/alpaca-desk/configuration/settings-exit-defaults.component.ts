import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { components } from '../../../../api/broker.types';

type ExitTermsInput = components['schemas']['ExitTermsInput'];

/** What the Settings page knows about the defaults for new bots. */
export type ExitDefaultsView =
  | { readonly kind: 'loading' }
  | { readonly kind: 'unavailable' }
  /** No broker connection is in use, so there is no profile to hold defaults. */
  | { readonly kind: 'none' }
  | {
      readonly kind: 'ready';
      /** `null` when the profile in use saved no defaults. */
      readonly terms: ExitTermsInput | null;
      readonly profileName: string | null;
    };

/**
 * Defaults for new bots (PRD #2560): the exit terms Deploy pre-fills.
 *
 * Read-only here, and deliberately so. The defaults are saved with the broker
 * connection profile in use (its effective revision's `default_exit_terms`,
 * the same values the Deploy read serves from the bound worker), so a change
 * is a new revision of that profile, followed by Apply and a restart. The
 * button opens that editor rather than pretending the change applies at once.
 */
@Component({
  selector: 'app-settings-exit-defaults',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './settings-exit-defaults.component.html',
  styleUrl: './settings-exit-defaults.component.scss',
  host: { class: 'block' },
})
export class SettingsExitDefaultsComponent {
  readonly defaults = input.required<ExitDefaultsView>();
  readonly changeRequested = output();
}
