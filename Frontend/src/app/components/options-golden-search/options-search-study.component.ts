import { ChangeDetectionStrategy, Component, computed, effect, input, output, signal, untracked } from '@angular/core';
import { form, FormField } from '@angular/forms/signals';
import { RouterLink } from '@angular/router';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { OptionsSearchCandidateComponent } from './options-search-candidate.component';
import type { OptionsStudy, SearchCommand, SessionLoader } from './options-golden-search.types';

export interface StudyAction {
  readonly command: SearchCommand;
  readonly candidate_hashes?: readonly string[];
  readonly candidate_hash?: string;
  readonly comment?: string;
  readonly waived_gate_ids?: readonly string[];
}

@Component({
  selector: 'app-options-search-study',
  imports: [AssetIdentityComponent, FormField, ReceiptLabelPipe, RouterLink, OptionsSearchCandidateComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-search-study.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsSearchStudyComponent {
  readonly study = input.required<OptionsStudy>();
  readonly step = input.required<number>();
  readonly busy = input(false);
  readonly action = output<StudyAction>();
  readonly sessionLoader = input<SessionLoader | null>(null);
  protected readonly selection = signal({ hash: '', comment: '' });
  protected readonly fields = form(this.selection);
  protected readonly shortlist = signal<readonly string[] | null>(null);
  protected readonly recommendations = computed(() => this.study().candidates.filter((candidate) => candidate.is_original || candidate.verdict === 'meets_rules'));
  protected readonly rejected = computed(() => this.study().candidates.filter((candidate) => !candidate.is_original && candidate.verdict !== 'meets_rules'));
  protected readonly showRejected = signal(false);
  protected readonly selectedHashes = computed(() => this.shortlist() ?? this.study().candidates.filter((candidate) => !candidate.is_original && candidate.verdict === 'meets_rules').map((candidate) => candidate.candidate_hash));
  protected readonly approvalCandidate = computed(() => this.study().final_results.find((candidate) => candidate.candidate_hash === this.selection().hash) ?? null);
  protected readonly approvalReview = computed(() => this.study().approval_reviews[this.selection().hash] ?? null);
  protected readonly acknowledged = signal<ReadonlySet<string>>(new Set());
  protected readonly canApprove = computed(() => {
    const review = this.approvalReview();
    return this.allowed('approve') && review !== null && review.blocked_reason === null && review.failures.length === 0;
  });
  protected readonly canExcept = computed(() => {
    const review = this.approvalReview();
    return this.allowed('approve_exception') && review !== null && review.blocked_reason === null && review.failures.length > 0 && this.selection().comment.trim() !== '' && review.failures.every((gate) => this.acknowledged().has(gate.gate_id)) && this.acknowledged().size === review.failures.length;
  });
  private readonly acknowledgementIdentity = computed(() => JSON.stringify([this.study().id, this.selection().hash, this.approvalReview()]));

  private readonly identity = computed(() => this.study().id);

  constructor() {
    effect(() => {
      this.acknowledgementIdentity();
      untracked(() => this.acknowledged.set(new Set()));
    });
    effect(() => {
      this.identity();
      untracked(() => { this.shortlist.set(null); this.selection.set({ hash: '', comment: '' }); });
    });
    effect(() => {
      const results = this.study().final_results;
      untracked(() => {
        if (this.selection().hash === '' && results.length > 0) this.selection.update((value) => ({ ...value, hash: results[0].candidate_hash }));
      });
    });
  }

  protected allowed(command: SearchCommand): boolean {
    return !this.busy() && this.study().allowed_commands.includes(command);
  }

  protected toggleRejected(event: Event): void {
    if (event.target instanceof HTMLDetailsElement) this.showRejected.set(event.target.open);
  }

  protected toggleFinalist(hash: string, event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    this.shortlist.set(event.target.checked ? [...this.selectedHashes(), hash] : this.selectedHashes().filter((item) => item !== hash));
  }

  protected acknowledge(gateId: string, event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    const next = new Set(this.acknowledged());
    if (event.target.checked) next.add(gateId); else next.delete(gateId);
    this.acknowledged.set(next);
  }

  protected ask(command: SearchCommand): void {
    if (!this.allowed(command)) return;
    if (command === 'approve' && !this.canApprove()) return;
    if (command === 'approve_exception' && !this.canExcept()) return;
    const value = this.selection();
    this.action.emit({ command, ...(command === 'open_final' ? { candidate_hashes: this.selectedHashes() } : {}), ...(['approve', 'approve_exception'].includes(command) ? { candidate_hash: value.hash, comment: value.comment.trim() } : {}), ...(command === 'approve_exception' ? { waived_gate_ids: this.approvalReview()?.failures.map((gate) => gate.gate_id) } : {}) });
  }
}
