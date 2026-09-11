import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, output, signal } from '@angular/core';
import { FormField, form } from '@angular/forms/signals';

import type { BrokerCredentialSlot } from '../../../../api/alpaca.types';
import type { RevisionContent } from './broker-configuration.service';
import { ConfigurationRevisionFormComponent } from './configuration-revision-form.component';
import {
  type RevisionDraft,
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
  /**
   * The slot a new draft starts on, so the picker never opens on a name that is
   * not on the allowlist.
   *
   * It is a `computed` with an explicit `equal` rather than a `linkedSignal`
   * source function, because a source function re-seeds whenever its
   * *dependencies* change even if its value does not: `slots.reload()` — which
   * the page's own "Reload configuration" button issues — hands this component
   * an equal-but-new array, and that would throw away a half-typed profile.
   */
  private readonly seedSlot = computed(() => preferredSlot(this.slots()), {
    equal: (a, b) => a === b,
  });
  protected readonly draft = linkedSignal<string, RevisionDraft>({
    source: this.seedSlot,
    computation: (slot) => emptyDraft(slot),
  });
  protected readonly draftForm = form(this.draft);

  protected readonly problems = computed(() => {
    const named = this.displayName().trim().length > 0 ? [] : ['Give the profile a name.'];
    return [...named, ...draftProblems(this.draft(), this.slots())];
  });

  // `draftForm().invalid()` covers what the draft cannot see: Angular's number
  // accessor rejects a malformed entry without writing the model, leaving the
  // previously typed value in place while the field shows something else.
  protected readonly canSave = computed(
    () => !this.busy() && this.problems().length === 0 && !this.draftForm().invalid(),
  );

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
