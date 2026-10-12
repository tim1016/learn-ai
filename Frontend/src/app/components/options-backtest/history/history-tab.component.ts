import { afterNextRender, ChangeDetectionStrategy, Component, computed, DestroyRef, effect, ElementRef, inject, Injector, input, output, resource, signal, untracked, viewChild } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { ConfirmDeleteComponent } from '../../../shared/research-record/confirm-delete.component';
import { RecordPoller } from '../../../shared/research-record/record-poller';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { extractServerMessage } from '../../broker/operation-error';
import { OptionsBacktestService, RunRefusedError } from '../options-backtest.service';
import type { RunListItem, ServedSetup } from '../options-backtest.types';
import { RUN_POLL_MS } from '../run-poll';
import { CompareViewComponent } from '../run-detail/compare/compare-view.component';
import { RunPanelComponent } from '../run/run-panel.component';

let nextHintId = 0;

/**
 * This page's runs, newest first as the server lists them: when each was
 * made, how it ended, the setup it ran and the dates, and the headline
 * figures of a run that is complete. A run can be opened, which shows it as
 * the Backtest tab does (progress, result, why it failed) with the setup it
 * used, or deleted; the server refuses to delete a run that is going and says
 * so. The list is read again each time the tab is shown, and every few seconds
 * while it holds a run that is going, so that a run that ends shows as ended
 * and can be deleted without leaving the tab.
 */
@Component({
  selector: 'app-options-backtest-history',
  imports: [ButtonModule, AssetIdentityComponent, AuthoredUsdPipe, ReceiptLabelPipe, ConfirmDeleteComponent, TimestampDisplayComponent, RunPanelComponent, CompareViewComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './history-tab.component.html',
  styleUrl: './history-tab.component.scss',
})
export class HistoryTabComponent {
  private readonly service = inject(OptionsBacktestService);
  private readonly injector = inject(Injector);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly pollMs = inject(RUN_POLL_MS);
  private readonly poller = new RecordPoller(inject(DestroyRef));

  /** Whether the tab is the one on show; the list is read again each time it becomes so. */
  readonly active = input(true);
  /** The owner asked for the setup of a run to be put into the Backtest tab's form. */
  readonly useSetup = output<ServedSetup>();
  /** A run was deleted: the Backtest tab must not go on showing it. */
  readonly deleted = output<number>();

  private readonly list = resource({ loader: () => this.service.runs() });
  protected readonly runs = computed<RunListItem[]>(() => (this.list.hasValue() ? this.list.value() : []));
  protected readonly loading = computed(() => !this.list.hasValue() && this.list.error() === undefined);
  protected readonly listProblem = computed(() => {
    const error: unknown = this.list.error();
    return error === undefined ? null : extractServerMessage(error, 'The runs could not be read.');
  });
  protected readonly opened = signal<RunListItem | null>(null);
  /** The two runs on screen side by side, in the order they were ticked. */
  protected readonly comparing = signal<{ readonly a: number; readonly b: number } | null>(null);
  protected readonly hintId = `ob-compare-hint-${nextHintId++}`;
  /** Every run the owner has ticked, in the order ticked, whether or not it is still listed. */
  private readonly ticked = signal<readonly number[]>([]);
  /** The ticked runs that are still listed and complete: a run deleted or not complete cannot be compared. */
  private readonly chosen = computed(() => this.ticked().filter((id) => this.runs().some((run) => run.id === id && run.status === 'complete')));
  protected readonly canCompare = computed(() => this.chosen().length === 2);
  protected readonly compareHint = computed(() => {
    const ids = this.chosen();
    if (ids.length === 0) return 'Tick two complete runs to compare them.';
    if (ids.length === 1) return 'Tick one more complete run.';
    if (ids.length === 2) return `Run ${ids[0]} and run ${ids[1]} are ticked.`;
    return 'More than two runs are ticked. Untick until two are left.';
  });
  protected readonly deleteProblem = signal<string | null>(null);
  private readonly panel = viewChild(RunPanelComponent);
  private readonly compareView = viewChild(CompareViewComponent);
  private readonly compareButton = viewChild<ElementRef<HTMLElement>>('compareButton');
  private readonly heading = viewChild<ElementRef<HTMLElement>>('heading');
  /** Where the keyboard goes once the list has been read again after a delete: the row that stood at this place, else the run if it is still there. */
  private focusAfterList: { readonly index: number; readonly id: number } | null = null;
  /** Whether the list, as last read, holds a run that is going. A list that cannot be read again leaves it as it was. */
  private anyGoing = false;

  constructor() {
    effect(() => {
      if (this.active()) untracked(() => this.list.reload());
    });
    effect(() => {
      const status = this.list.status();
      if (status === 'resolved' || status === 'error') untracked(() => this.focusAfterDelete());
    });
    // The list is on show (the tab is, and no run is open over it): while a run in it is going, read it again every few seconds.
    effect(() => {
      const status = this.list.status();
      const onShow = this.active() && this.opened() === null && this.comparing() === null;
      untracked(() => this.lookAgainWhileAnyGoes(status, onShow));
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

  protected compareLabel(id: number): string {
    return `Compare run ${id}`;
  }

  protected isTicked(id: number): boolean {
    return this.chosen().includes(id);
  }

  /** Ticks or unticks one run. A third tick unticks nothing: the hint says what to do. */
  protected tick(id: number, event: Event): void {
    const box = event.target;
    if (!(box instanceof HTMLInputElement)) return;
    this.ticked.update((ids) => (box.checked ? (ids.includes(id) ? ids : [...ids, id]) : ids.filter((each) => each !== id)));
  }

  protected compare(): void {
    const [a, b] = this.chosen();
    if (this.chosen().length !== 2) return;
    this.comparing.set({ a, b });
    afterNextRender(() => this.compareView()?.focusHeading(), { injector: this.injector });
  }

  /** Back to the list, with the keyboard on the Compare button that opened the comparison (or on the list's heading if it is gone). */
  protected closeCompare(): void {
    this.comparing.set(null);
    this.list.reload();
    afterNextRender(() => (this.compareButton() ?? this.heading())?.nativeElement.focus(), { injector: this.injector });
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
    // The Confirm delete that was pressed is gone with the answer, and the row may be too: the keyboard goes to a row that is still there.
    this.focusAfterList = { index: this.runs().findIndex((row) => row.id === id), id };
    try {
      await this.service.deleteRun(id);
      this.deleted.emit(id);
    } catch (error) {
      this.deleteProblem.set(error instanceof RunRefusedError ? error.message : extractServerMessage(error, 'The run could not be deleted. Try again.'));
    }
    this.list.reload();
  }

  /** The list has been read again after a delete: the Open button of the run if it is still listed, else of the row now at its place, else the heading. */
  private focusAfterDelete(): void {
    const pending = this.focusAfterList;
    if (pending === null) return;
    this.focusAfterList = null;
    const rows = this.runs();
    const row = rows.find((candidate) => candidate.id === pending.id) ?? rows[pending.index] ?? rows.at(-1);
    afterNextRender(
      () => {
        const open = row === undefined ? null : this.host.nativeElement.querySelector<HTMLElement>(`button[aria-label="${this.openLabel(row.id)}"]`);
        (open ?? this.heading()?.nativeElement)?.focus();
      },
      { injector: this.injector },
    );
  }

  private lookAgainWhileAnyGoes(status: string, onShow: boolean): void {
    if (status === 'resolved') this.anyGoing = this.runs().some((run) => run.status === 'running');
    if (!onShow || !this.anyGoing) this.poller.stop();
    else if (status === 'resolved' || status === 'error') this.poller.schedule(this.pollMs, () => this.list.reload());
  }
}
