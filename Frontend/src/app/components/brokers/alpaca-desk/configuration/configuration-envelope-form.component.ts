import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';

import { ENVELOPE_LABELS, type RevisionDraft } from './configuration-revision-draft';

/**
 * Profile startup loss defaults and extended-hours offsets. Effective account
 * loss limits change only through Settings' Daily loss limit; existing deployments keep
 * their immutable exit terms. New fields stay empty until explicitly chosen.
 * The shared offset fields preserve typed values when the endpoint switches.
 */
@Component({
  selector: 'app-configuration-envelope-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormField],
  templateUrl: './configuration-envelope-form.component.html',
  styleUrl: './configuration-envelope-form.component.scss',
  host: { class: 'block' },
})
export class ConfigurationEnvelopeFormComponent {
  readonly draftForm = input.required<FieldTree<RevisionDraft>>();
  protected readonly labels = ENVELOPE_LABELS;
  protected readonly isLive = computed(
    () => this.draftForm().endpoint_mode().value() === 'live',
  );
}
