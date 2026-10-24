import { ChangeDetectionStrategy, Component, computed, inject, input, output, resource, signal } from '@angular/core';
import { disabled, FormField, form, minLength, required } from '@angular/forms/signals';
import { RouterLink } from '@angular/router';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { extractServerMessage } from '../../broker/operation-error';
import { OptionsQualificationEvidenceComponent } from '../options-qualification-evidence.component';
import { GoldenOptionSetupsService, type GoldenOptionSetup } from '../golden-option-setups.service';

/**
 * Approves one completed run, or revokes and re-proves the version that run made. A run's own record is the
 * subject, never the setup being edited anywhere else: the run is the `runId`, and the content the owner reviewed
 * is `expectedSetupSha256`, which the server holds the approval to. A repeated click, a retry and a list read
 * before another approval was made cannot make a second version: the command is one request under one key until
 * what it says changes, and a run that already has a version that is not revoked is shown that version instead.
 */
@Component({
  selector: 'app-golden-option-setup-approval',
  imports: [FormField, ReceiptLabelPipe, OptionsQualificationEvidenceComponent, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-setup-approval.component.html',
  styleUrl: './golden-setup-approval.component.scss',
})
export class GoldenSetupApprovalComponent {
  readonly runId = input.required<number>();
  /** The hash of the content the owner reviewed in the run, sent with every approval so any other content is refused. Required: no approval is made without it. */
  readonly expectedSetupSha256 = input.required<string>();
  /** A version was approved, revoked or re-proved, or found to exist already. */
  readonly changed = output<GoldenOptionSetup>();
  protected readonly noteId = computed(() => `golden-note-${this.runId()}`);
  private readonly service = inject(GoldenOptionSetupsService);
  private readonly note = signal('');
  protected readonly noteForm = form(this.note, (text) => {
    required(text);
    minLength(text, 3);
    disabled(text, () => this.busy());
  });
  protected readonly busy = signal(false);
  protected readonly problem = signal<string | null>(null);
  /** Said when the command did nothing because what it asked for was already so. Not a failure. */
  protected readonly notice = signal<string | null>(null);
  private readonly made = signal<GoldenOptionSetup | null>(null);
  protected readonly versions = resource({
    params: () => this.runId(),
    loader: ({ params }) => this.service.list(params),
  });
  protected readonly golden = computed(() => {
    const made = this.made();
    if (made?.source_run_id === this.runId()) return made;
    return this.versions.hasValue()
      ? this.versions.value().find((version) => version.source_run_id === this.runId()) ?? null : null;
  });
  protected readonly readProblem = computed(() => this.versions.error() === undefined
    ? null : extractServerMessage(this.versions.error(), 'Golden setups could not be read. Try again.'));
  private command: { context: string; key: string } | null = null;

  protected async act(kind: 'approve' | 'revoke' | 'reprove'): Promise<void> {
    if (this.busy() || (kind !== 'reprove' && this.noteForm().invalid())) return;
    const runId = this.runId();
    const golden = this.golden();
    const note = this.note().trim();
    const expected = this.expectedSetupSha256();
    const context = JSON.stringify({ kind, runId, golden: golden?.id, expected, note: kind === 'reprove' ? null : note });
    if (this.command?.context !== context) this.command = { context, key: crypto.randomUUID() };
    this.busy.set(true);
    this.problem.set(null);
    this.notice.set(null);
    try {
      const key = this.command.key;
      let result: GoldenOptionSetup | null = null;
      if (kind === 'approve') {
        // The list on screen may be older than another approval of this run; this one is read now.
        result = (await this.service.list(runId)).find((version) => version.status !== 'revoked') ?? null;
        if (result !== null) this.notice.set('This run was already approved. That version is shown, and nothing more was approved.');
        else result = await this.service.approve(runId, note, key, expected);
      } else if (golden !== null) {
        result = kind === 'reprove' ? await this.service.reprove(golden.id, key) : await this.service.revoke(golden.id, note, key);
      }
      if (this.runId() === runId && result !== null) {
        this.made.set(result);
        this.changed.emit(result);
      }
    } catch (error) {
      if (this.runId() === runId) this.problem.set(extractServerMessage(error, 'This qualification could not be recorded. Retry to recover the same request.'));
    } finally {
      this.busy.set(false);
    }
  }
}
