import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, output, signal } from '@angular/core';
import { FormField, form } from '@angular/forms/signals';

import type { BrokerCredentialSlot } from '../../../../api/alpaca.types';
import type { RevisionContent } from './broker-configuration.service';
import { ConfigurationRevisionFormComponent } from './configuration-revision-form.component';
import {
  draftProblems,
  emptyDraft,
  preferredSlot,
  toRevisionContent,
} from './configuration-revision-draft';

/**
 * Create a profile and its first revision. Nothing here stages or applies
 * anything: a saved profile is inert until it is staged, applied, and the
 * worker is restarted.
 */
@Component({
  selector: 'app-configuration-profile-create',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ConfigurationRevisionFormComponent, FormField],
  templateUrl: './configuration-profile-create.component.html',
  styleUrl: './configuration-profile-create.component.scss',
  host: { class: 'block' },
})
export class ConfigurationProfileCreateComponent {
  readonly slots = input.required<readonly BrokerCredentialSlot[]>();
  readonly busy = input(false);

  readonly created = output<{ displayName: string; content: RevisionContent }>();

  protected readonly open = signal(false);
  protected readonly displayName = signal('');
  protected readonly nameForm = form(this.displayName);
  // Re-seeded when the slot list arrives so the picker never starts on a slot
  // name that is not on the allowlist. Slots are read once per page load, so
  // this cannot discard an edit in progress.
  protected readonly draft = linkedSignal(() => emptyDraft(preferredSlot(this.slots())));
  protected readonly draftForm = form(this.draft);

  protected readonly problems = computed(() => {
    const named = this.displayName().trim().length > 0 ? [] : ['Give the profile a name.'];
    return [...named, ...draftProblems(this.draft())];
  });

  protected readonly canSave = computed(() => !this.busy() && this.problems().length === 0);

  protected submit(): void {
    if (!this.canSave()) return;
    this.created.emit({
      displayName: this.displayName().trim(),
      content: toRevisionContent(this.draft()),
    });
  }

  /** Called by the page once a create has actually landed, never optimistically. */
  reset(): void {
    this.open.set(false);
    this.displayName.set('');
    this.draft.set(emptyDraft(preferredSlot(this.slots())));
  }
}
