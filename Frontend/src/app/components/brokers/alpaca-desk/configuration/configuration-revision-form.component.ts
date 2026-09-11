import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';

import type { BrokerCredentialSlot } from '../../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { ConfigurationEnvelopeFormComponent } from './configuration-envelope-form.component';
import type { RevisionDraft } from './configuration-revision-draft';

/**
 * The editable content of one revision: which credential slot it names, which
 * endpoint it talks to, and — for a live endpoint — the six envelope values.
 *
 * The slot picker shows every slot on the code-owned allowlist, including the
 * ones with no credential pair injected, and says so on the option itself. A
 * slot the operator cannot see is a slot they cannot reason about; what they
 * must not see is anything about the credentials themselves, and this surface
 * carries only a label and a boolean.
 */
@Component({
  selector: 'app-configuration-revision-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ConfigurationEnvelopeFormComponent, FormField, ReceiptLabelPipe],
  templateUrl: './configuration-revision-form.component.html',
  styleUrl: './configuration-revision-form.component.scss',
  host: { class: 'block' },
})
export class ConfigurationRevisionFormComponent {
  readonly draftForm = input.required<FieldTree<RevisionDraft>>();
  readonly slots = input.required<readonly BrokerCredentialSlot[]>();
  readonly problems = input<readonly string[]>([]);

  protected readonly isLive = computed(
    () => this.draftForm().endpoint_mode().value() === 'live',
  );

  protected readonly chosenSlot = computed(() => {
    const slot = this.draftForm().credential_slot().value();
    return this.slots().find((candidate) => candidate.slot === slot) ?? null;
  });

  /** True only when the slot is known *and* has no injected pair — not while loading. */
  protected readonly slotUnavailable = computed(() => this.chosenSlot()?.available === false);

  /**
   * The draft's slot when the directory has been read and does not list it —
   * a revision saved against a slot this deployment has since dropped. `null`
   * while the directory is unread, because "not listed" is then unknown.
   */
  protected readonly unlistedSlot = computed(() => {
    const slot = this.draftForm().credential_slot().value();
    return this.slots().length > 0 && slot.length > 0 && this.chosenSlot() === null ? slot : null;
  });
}
