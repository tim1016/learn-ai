import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, resource } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { extractServerMessage } from '../broker/operation-error';
import { OptionsResearchLineageService } from './options-research-lineage.service';

@Component({
  selector: 'app-options-research-trials',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-research-trials.component.html',
  styleUrl: './options-research-lineage.component.scss',
})
export class OptionsResearchTrialsComponent {
  readonly studyId = input.required<string>();
  private readonly service = inject(OptionsResearchLineageService);
  protected readonly cursor = linkedSignal({ source: this.studyId, computation: () => '0' });
  protected readonly previous = linkedSignal<string, readonly string[]>({ source: this.studyId, computation: () => [] });
  protected readonly page = resource({ params: () => ({ id: this.studyId(), after: this.cursor() }), loader: ({ params }) => this.service.trials(params.id, params.after) });
  protected readonly problem = computed(() => this.page.error() === undefined ? null : extractServerMessage(this.page.error(), 'The retained trial ledger could not be read.'));

  protected next(cursor: string): void {
    this.previous.update(values => [...values, this.cursor()]);
    this.cursor.set(cursor);
  }

  protected back(): void {
    const values = this.previous();
    this.cursor.set(values.at(-1) ?? '0');
    this.previous.set(values.slice(0, -1));
  }
}
