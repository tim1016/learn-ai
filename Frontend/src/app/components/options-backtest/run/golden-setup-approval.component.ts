import { ChangeDetectionStrategy, Component, computed, inject, input, resource, signal } from '@angular/core';
import { disabled, FormField, form, minLength, required } from '@angular/forms/signals';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { extractServerMessage } from '../../broker/operation-error';
import { GoldenOptionSetupsService, type GoldenOptionSetup } from '../golden-option-setups.service';

@Component({
  selector: 'app-golden-option-setup-approval',
  imports: [FormField, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-setup-approval.component.html',
  styleUrl: './golden-setup-approval.component.scss',
})
export class GoldenSetupApprovalComponent {
  readonly runId = input.required<number>();
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
  private readonly changed = signal<GoldenOptionSetup | null>(null);
  protected readonly versions = resource({
    params: () => this.runId(),
    loader: ({ params }) => this.service.list(params),
  });
  protected readonly golden = computed(() => {
    const changed = this.changed();
    if (changed?.source_run_id === this.runId()) return changed;
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
    const context = JSON.stringify({ kind, runId, golden: golden?.id, note: kind === 'reprove' ? null : note });
    if (this.command?.context !== context) this.command = { context, key: crypto.randomUUID() };
    this.busy.set(true);
    this.problem.set(null);
    try {
      const key = this.command.key;
      const result = kind === 'approve'
        ? await this.service.approve(runId, note, key)
        : kind === 'reprove' && golden !== null
          ? await this.service.reprove(golden.id, key)
          : golden !== null ? await this.service.revoke(golden.id, note, key) : null;
      if (this.runId() === runId && result !== null) this.changed.set(result);
    } catch (error) {
      if (this.runId() === runId) this.problem.set(extractServerMessage(error, 'This qualification could not be recorded. Retry to recover the same request.'));
    } finally {
      this.busy.set(false);
    }
  }
}
