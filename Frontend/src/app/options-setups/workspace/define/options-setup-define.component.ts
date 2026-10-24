import { afterNextRender, ChangeDetectionStrategy, Component, computed, effect, ElementRef, inject, Injector, signal, untracked, viewChild } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { BottomBarComponent } from '../../../components/options-backtest/cards/bottom-bar.component';
import { CostsCardComponent } from '../../../components/options-backtest/cards/costs-card.component';
import { EntryCardComponent } from '../../../components/options-backtest/cards/entry-card.component';
import { InstrumentCardComponent } from '../../../components/options-backtest/cards/instrument-card.component';
import { LegsCardComponent } from '../../../components/options-backtest/cards/legs-card.component';
import { SummaryComponent } from '../../../components/options-backtest/cards/summary.component';
import { PositionPreviewComponent } from '../../../components/options-backtest/position/position-preview.component';
import { bringIntoView, legBlock } from '../../../components/options-backtest/read-back-scroll';
import { OptionsSetupWorkspaceStore } from '../options-setup-store';
import { describeStanding } from '../draft-standing';

/**
 * Define: the timed, rule-based setup an options bot is built from, with the
 * server reading it back in plain sentences (PRD #3490 R21). The saved name in
 * the address says which setup the draft is the working copy of; the draft
 * itself lives in the workspace's store, so a tab switch loses none of it.
 *
 * Export and Import write and read the setup file; Save writes it to the
 * saved-name library, under the name in the bar. Saving under another name
 * adds a library entry and approves nothing.
 */
@Component({
  selector: 'app-options-setup-define',
  imports: [ButtonModule, InstrumentCardComponent, EntryCardComponent, LegsCardComponent, PositionPreviewComponent, CostsCardComponent, SummaryComponent, BottomBarComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-setup-define.component.html',
  styleUrl: './options-setup-define.component.scss',
})
export class OptionsSetupDefineComponent {
  protected readonly store = inject(OptionsSetupWorkspaceStore);
  private readonly injector = inject(Injector);

  protected readonly standing = computed(() => describeStanding(this.store.standing()));
  private readonly legsCard = viewChild(LegsCardComponent);
  private readonly readBackPane = viewChild<ElementRef<HTMLElement>>('readBackPane');
  /** The leg that took the keyboard while the read-back had no sentences. It is shown the once the read-back has them. */
  private legAwaitingReadBack: string | null = null;
  /** How tall the bar of actions is, which the end of the page leaves clear when the bar is fixed to the window. */
  protected readonly barHeight = signal(0);

  constructor() {
    // Once the server has read the setup back, a leg that took the keyboard meanwhile is shown in the read-back.
    effect(() => {
      if (this.store.summary().kind !== 'ready') return;
      untracked(() => {
        const waiting = this.legAwaitingReadBack;
        this.legAwaitingReadBack = null;
        if (waiting !== null) this.afterRender(() => this.scrollReadBackTo(waiting));
      });
    });
  }

  protected async importFile(event: Event): Promise<void> {
    const chooser = event.target;
    if (!(chooser instanceof HTMLInputElement)) return;
    const file = chooser.files?.[0] ?? null;
    chooser.value = '';
    if (file !== null) await this.store.importFile(file);
  }

  protected addLeg(): void {
    this.store.addLeg();
  }

  /** The copy stands just after its leg; the keyboard goes to it, so the owner lands on what was made. */
  protected copyLeg(legId: string): void {
    const at = this.store.draft().legs.findIndex((leg) => leg.id === legId);
    this.store.copyLeg(legId);
    if (at >= 0) this.afterRender(() => this.legsCard()?.focusHeading(at + 1));
  }

  /** The keyboard goes to where the deleted leg stood: the next leg's delete control, else the last one's, else Add leg. */
  protected removeLeg(legId: string): void {
    const at = this.store.draft().legs.findIndex((leg) => leg.id === legId);
    this.store.removeLeg(legId);
    const left = this.store.draft().legs.length;
    if (at < 0) return;
    this.afterRender(() => (left === 0 ? this.legsCard()?.focusAdd() : this.legsCard()?.focusDelete(Math.min(at, left - 1))));
  }

  /**
   * A leg took the keyboard: the rail shows where the server says that leg, so a long read-back is not scrolled by hand.
   * While the setup is being read back again there are no sentences to show, so the leg is shown once there are.
   */
  protected showInReadBack(legId: string): void {
    this.legAwaitingReadBack = this.scrollReadBackTo(legId) ? null : legId;
  }

  private scrollReadBackTo(legId: string): boolean {
    const pane = this.readBackPane()?.nativeElement;
    const block = pane === undefined ? null : legBlock(pane, legId);
    if (pane === undefined || block === null) return false;
    bringIntoView(pane, block);
    return true;
  }

  private afterRender(move: () => void): void {
    afterNextRender(move, { injector: this.injector });
  }
}
