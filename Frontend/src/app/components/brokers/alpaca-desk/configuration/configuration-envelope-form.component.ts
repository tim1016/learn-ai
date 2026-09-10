import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';

import { ENVELOPE_LABELS, type RevisionDraft } from './configuration-revision-draft';

/**
 * The six live-envelope values of one revision draft.
 *
 * Every field starts empty and stays empty until the operator types a number:
 * ADR 0059 Decision 4 admits no default for any of the six, and a pre-filled
 * form is a default with a nicer name.
 *
 * Saving these values changes nothing that governs money. They reach a running
 * bot only through the arming ceremony the owner runs on the host, and this
 * page cannot start it — which is what the note in the template says, on the
 * form itself rather than in a tooltip.
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
}
