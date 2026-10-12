import { afterNextRender, ChangeDetectionStrategy, Component, computed, effect, ElementRef, inject, Injector, input, output, resource, signal, untracked, viewChild } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { ConfirmDeleteComponent } from '../../../shared/research-record/confirm-delete.component';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { extractServerMessage } from '../../broker/operation-error';
import { OptionsBacktestService, RunRefusedError } from '../options-backtest.service';
import type { RunListItem, ServedSetup } from '../options-backtest.types';
import { RunPanelComponent } from '../run/run-panel.component';

/**
 * This page's runs, newest first as the server lists them: when each was
 * made, how it ended, the setup it ran and the dates, and the headline
 * figures of a run that is complete. A run can be opened, which shows it as
 * the Backtest tab does (progress, result, why it failed) with the setup it
 * used, or deleted; the server refuses to delete a run that is going and says
 * so. The list is read again each time the tab is shown.
 */
@Component({
  selector: 'app-options-backtest-history',
  imports: [ButtonModule, AssetIdentityComponent, AuthoredUsdPipe, ReceiptLabelPipe, ConfirmDeleteComponent, TimestampDisplayComponent, RunPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './history-tab.component.html',
  styleUrl: './history-tab.component.scss',
})
export class HistoryTabComponent {
  private readonly service = inject(OptionsBacktestService);
  private readonly injector = inject(Injector);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  /** Whether the tab is the one on show; the list is read again each time it becomes so. */
  readonly active = input(true);
  /** The owner asked for the setup of a run to be put into the Backtest tab's form. */
  readonly useSetup = output<ServedSetup>();

  private readonly list = resource({ loader: () => this.service.runs() });
  protected readonly runs = computed<RunListItem[]>(() => (this.list.hasValue() ? this.list.value() : []));
  protected readonly loading = computed(() => !this.list.hasValue() && this.list.error() === undefined);
  protected readonly listProblem = computed(() => {
    const error: unknown = this.list.error();
    return error === undefined ? null : extractServerMessage(error, 'The runs could not be read.');
  });
  protected readonly opened = signal<RunListItem | null>(null);
  protected readonly deleteProblem = signal<string | null>(null);
  private readonly panel = viewChild(RunPanelComponent);

  constructor() {
    effect(() => {
      if (this.active()) untracked(() => this.list.reload());
    });
  }

  protected retry(): void {
    this.list.reload();
  }

  /** The names of a row's two controls; a template adds no strings to a row's id on a surface that shows dollars. */
  protected openLabel(id: number): string {
    return `Open run ${id}`;
  }

  protected deleteLabel(id: number): string {
    return `Delete run ${id}`;
  }

  protected open(run: RunListItem): void {
    this.opened.set(run);
    afterNextRender(() => this.panel()?.focusHeading(), { injector: this.injector });
  }

  /** Back to the list, with the keyboard on the Open button of the run that was open, so the owner does not start again from the top. */
  protected close(): void {
    const id = this.opened()?.id;
    this.opened.set(null);
    this.list.reload();
    if (id === undefined) return;
    afterNextRender(() => this.host.nativeElement.querySelector<HTMLElement>(`button[aria-label="${this.openLabel(id)}"]`)?.focus(), { injector: this.injector });
  }

  protected async remove(id: number): Promise<void> {
    this.deleteProblem.set(null);
    try {
      await this.service.deleteRun(id);
    } catch (error) {
      this.deleteProblem.set(error instanceof RunRefusedError ? error.message : extractServerMessage(error, 'The run could not be deleted. Try again.'));
    }
    this.list.reload();
  }
}
