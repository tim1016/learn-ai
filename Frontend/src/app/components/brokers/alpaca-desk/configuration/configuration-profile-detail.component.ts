import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, output, signal } from '@angular/core';
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
  readonly revisions = input.required<readonly BrokerProfileRevision[]>();
  readonly slots = input.required<readonly BrokerCredentialSlot[]>();
  readonly observed = input<readonly BrokerObservedAccount[] | null>(null);
  readonly nickname = input<string | null>(null);
  readonly busy = input(false);

  readonly renamed = output<string>();
  readonly cloned = output<string>();
  readonly revisionSaved = output<RevisionSubmission>();
  readonly verifyRequested = output();
  readonly pinRequested = output<string>();
  readonly nicknameSubmitted = output<string>();

  protected readonly latest = computed(() => this.detail().latest_revision);

  protected readonly renameDraft = linkedSignal(() => this.detail().profile.display_name);
  protected readonly renameForm = form(this.renameDraft);
  protected readonly cloneDraft = signal('');
  protected readonly cloneForm = form(this.cloneDraft);

  // Seeded from the revision this editor was opened against, so the operator
  // edits forward from the stored content rather than from a blank form.
  protected readonly revisionDraft = linkedSignal(() => {
    const latest = this.latest();
    return latest === null ? emptyDraft(preferredSlot(this.slots())) : draftFromRevision(latest);
  });
  protected readonly revisionForm = form(this.revisionDraft);

  protected readonly revisionProblems = computed(() => draftProblems(this.revisionDraft()));
  protected readonly canSaveRevision = computed(
    () => !this.busy() && this.revisionProblems().length === 0,
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
    this.cloneDraft.set('');
  }
}
