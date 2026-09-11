import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, output } from '@angular/core';
import { FormField, form } from '@angular/forms/signals';

import type {
  BrokerCredentialSlot,
  BrokerObservedAccount,
  BrokerProfileDetail,
  BrokerProfileRevision,
} from '../../../../api/alpaca.types';
import { ReceiptLabelPipe, formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { RevisionContent } from './broker-configuration.service';
import { ConfigurationAccountEvidenceComponent } from './configuration-account-evidence.component';
import { ConfigurationRevisionFormComponent } from './configuration-revision-form.component';
import {
  type RevisionDraft,
  draftFromRevision,
  draftProblems,
  emptyDraft,
  preferredSlot,
  toRevisionContent,
} from './configuration-revision-draft';

/** What a new revision is written against, so a stale edit conflicts rather than overwrites. */
export interface RevisionSubmission {
  readonly expectedRevision: number;
  readonly content: RevisionContent;
}

/**
 * One open profile: its label, its latest revision's content, the account that
 * revision is approved for, and the editor that writes the next revision.
 *
 * A revision is immutable, so "editing" is always writing the next one, and
 * the write carries the revision this editor was opened against. If another
 * tab has written one in the meantime the service refuses with
 * `revision_conflict` and nothing here is overwritten.
 */
@Component({
  selector: 'app-configuration-profile-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ConfigurationAccountEvidenceComponent,
    ConfigurationRevisionFormComponent,
    FormField,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  templateUrl: './configuration-profile-detail.component.html',
  styleUrl: './configuration-profile-detail.component.scss',
  host: { class: 'block' },
})
export class ConfigurationProfileDetailComponent {
  readonly detail = input.required<BrokerProfileDetail>();
  /** `null` when the history has not been read — never rendered as "(0)". */
  readonly revisions = input.required<readonly BrokerProfileRevision[] | null>();
  readonly slots = input.required<readonly BrokerCredentialSlot[]>();
  readonly observed = input<readonly BrokerObservedAccount[] | null>(null);
  readonly nickname = input<string | null>(null);
  readonly busy = input(false);

  readonly renamed = output<string>();
  readonly cloned = output<string>();
  readonly revisionSaved = output<RevisionSubmission>();
  readonly revisionStaged = output<BrokerProfileRevision>();
  readonly verifyRequested = output();
  readonly pinRequested = output<string>();
  readonly nicknameSubmitted = output<string>();

  protected readonly latest = computed(() => this.detail().latest_revision);

  // Every draft below is seeded from a `computed` carrying an explicit `equal`,
  // never from a `linkedSignal` source *function*. A source function re-seeds
  // whenever its dependencies change even if its value does not, and this
  // component is re-fed a fresh detail object after every write — so the
  // function form silently discards whatever the operator had typed.
  private readonly storedName = computed(() => this.detail().profile.display_name, {
    equal: (a, b) => a === b,
  });
  protected readonly renameDraft = linkedSignal<string, string>({
    source: this.storedName,
    computation: (name) => name,
  });
  protected readonly renameForm = form(this.renameDraft);

  private readonly openProfileId = computed(() => this.detail().profile.profile_id, {
    equal: (a, b) => a === b,
  });
  // Cleared when a different profile is open — which is what a *successful*
  // clone produces, since the page selects the new profile. A refused clone
  // leaves the same profile open and so keeps the name that was typed.
  protected readonly cloneDraft = linkedSignal<string, string>({
    source: this.openProfileId,
    computation: () => '',
  });
  protected readonly cloneForm = form(this.cloneDraft);

  /**
   * The seed for the editor, plus the identity that decides when re-seeding is
   * warranted. `equal` compares only the identity, so a rename or an account
   * approval — each of which reloads the detail and hands this component a
   * fresh object for the *same* revision — does not notify.
   *
   * The identity has to live here rather than in `linkedSignal`'s `source`,
   * because a `linkedSignal` computation is itself reactive: reading
   * `this.latest()` inside it would re-run it on every new detail object no
   * matter what the declared source said, and silently discard a half-typed
   * live envelope.
   */
  private readonly editorSeed = computed(
    () => {
      const latest = this.latest();
      const draft = latest === null
        ? emptyDraft(preferredSlot(this.slots()))
        : draftFromRevision(latest);
      return {
        identity: `${this.detail().profile.profile_id}#${latest?.revision ?? 0}#${draft.credential_slot}`,
        draft,
      };
    },
    { equal: (a, b) => a.identity === b.identity },
  );

  /**
   * Seeded from the revision this editor was opened against, so the operator
   * edits forward from the stored content rather than from a blank form. A
   * genuinely newer revision changes the identity, and re-seeding then is
   * correct — the draft was written against a revision that is no longer the
   * latest, and its `expected_revision` would be refused anyway.
   */
  protected readonly revisionDraft = linkedSignal<{ identity: string; draft: RevisionDraft }, RevisionDraft>({
    source: this.editorSeed,
    computation: (seed) => seed.draft,
  });
  protected readonly revisionForm = form(this.revisionDraft);

  protected readonly revisionProblems = computed(() =>
    draftProblems(this.revisionDraft(), this.slots()),
  );
  // The form's own validity matters as well as the draft's: Angular's number
  // accessor reports `badInput` (e.g. "0.02e") *without writing the model*, so
  // the draft would still hold the previously typed value and Save would submit
  // a real-money limit that differs from what is on screen.
  protected readonly canSaveRevision = computed(
    () => !this.busy() && this.revisionProblems().length === 0 && !this.revisionForm().invalid(),
  );
  protected readonly canRename = computed(
    () => !this.busy() && this.renameDraft().trim().length > 0,
  );
  protected readonly canClone = computed(() => !this.busy() && this.cloneDraft().trim().length > 0);

  /**
   * The operator-facing name for a slot the directory explains, falling back to
   * the stored key through `receiptLabel` when it does not — a revision can
   * name a slot the running deployment no longer lists.
   */
  protected slotLabel(slot: string): string {
    return this.slots().find((candidate) => candidate.slot === slot)?.label
      ?? formatReceiptLabel(slot);
  }

  protected submitRevision(): void {
    if (!this.canSaveRevision()) return;
    this.revisionSaved.emit({
      expectedRevision: this.latest()?.revision ?? 0,
      content: toRevisionContent(this.revisionDraft()),
    });
  }

  protected submitRename(): void {
    if (!this.canRename()) return;
    this.renamed.emit(this.renameDraft().trim());
  }

  protected submitClone(): void {
    if (!this.canClone()) return;
    this.cloned.emit(this.cloneDraft().trim());
  }
}
