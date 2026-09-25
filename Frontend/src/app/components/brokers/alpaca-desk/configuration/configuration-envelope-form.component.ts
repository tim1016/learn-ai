import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';

import { ENVELOPE_LABELS, type RevisionDraft } from './configuration-revision-draft';

/**
 * The risk values of one revision draft: a live draft's six envelope values,
 * or the two extended-hours offsets a paper draft may carry (#2440).
 *
 * Every field starts empty and stays empty until the operator types a number:
 * ADR 0059 Decision 4 admits no default for any of the six, and a pre-filled
 * form is a default with a nicer name. The paper offsets have no default for
 * the same reason; a regular-hours run's Start refuses until they are set.
 *
 * Saving the live values changes nothing that governs money. They reach a
 * running bot only through the arming ceremony the owner runs on the host, and
 * this page cannot start it — which is what the note in the template says, on
 * the form itself rather than in a tooltip. The paper offsets reach a running
 * bot when the revision is applied and the worker restarts, like every other
 * revision value; the paper note says that instead.
 *
 * The two offset inputs are the same draft fields in both modes, rendered once,
 * so switching the endpoint keeps what was typed.
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
