import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  accountWorkspaceSwitchRoute,
  type AccountWorkspaceLocation,
} from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneConfirmedAccount, laneDisplayName } from '../../../fleet/fleet-directory.types';
import {
  AlpacaLiveVerdictService,
  verdictModeChip,
} from '../../../services/alpaca-live-verdict.service';
import { AlpacaLaneModeChipComponent } from '../alpaca-desk/alpaca-lane-mode-chip.component';

/**
 * The account workspace's name — and the way to another account (ADR 0064
 * Decision 1).
 *
 * The name an account is shown by is the same everywhere (Decision 5), so this
 * reads it through `laneDisplayName` like every other surface, and its
 * Paper/Live mode stays a separate fact beside it through the one shared mode
 * chip — never folded into the name.
 *
 * Every Alpaca lane is listed, ready or not: choosing one is the operator's
 * explicit act, which is the only thing that may move them off the account
 * they are on (FR-096). Where a choice lands is the resolver's to decide —
 * including that nothing open over the workspace travels with it.
 */
@Component({
  selector: 'app-alpaca-account-switcher',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaLaneModeChipComponent, RouterLink],
  templateUrl: './alpaca-account-switcher.component.html',
  styleUrl: './alpaca-account-switcher.component.scss',
  host: {
    class: 'account-switcher',
    // A click anywhere else dismisses the list, and Escape does from anywhere
    // inside it — on the host, so the handler does not depend on which of the
    // trigger or the options has the keyboard. Declared here rather than
    // through `@HostListener`, per this app's Angular conventions.
    '(document:click)': 'onDocumentClick($event)',
    '(keydown.escape)': 'dismiss()',
  },
})
export class AlpacaAccountSwitcherComponent {
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly liveVerdicts = inject(AlpacaLiveVerdictService);
  private readonly host = inject(ElementRef<HTMLElement>);

  /** The workspace the operator is standing in: where a choice leaves from,
   * and which tab it keeps. */
  readonly location = input.required<AccountWorkspaceLocation>();

  /** The lens perspective a choice carries across, when the URL names one. */
  readonly lens = input<string | null>(null);

  private readonly trigger = viewChild.required<ElementRef<HTMLButtonElement>>('trigger');

  protected readonly open = signal(false);

  private readonly lanes = computed(() => this.fleetDirectory.lanesOf('alpaca'));

  /** The name of the account this workspace is, or `null` while the directory
   * has not resolved its lane — a bad deep link fails in place (FR-096). */
  protected readonly currentName = computed(() => {
    const lanes = this.lanes();
    const lane = lanes.find((candidate) => candidate.clerk_id === this.location().clerkId);
    return lane === undefined ? null : laneDisplayName(lane, lanes);
  });

  protected readonly options = computed(() => {
    const lanes = this.lanes();
    const location = this.location();
    const lens = this.lens();
    return lanes.map((lane) => ({
      clerkId: lane.clerk_id,
      name: laneDisplayName(lane, lanes),
      chip: verdictModeChip(this.liveVerdicts.stateFor(lane.clerk_id)),
      link: accountWorkspaceSwitchRoute(
        location,
        { clerkId: lane.clerk_id, accountId: laneConfirmedAccount(lane) },
        lens,
      ),
      isCurrent: lane.clerk_id === location.clerkId,
    }));
  });

  protected toggle(): void {
    this.open.update((open) => !open);
  }

  /** Dismiss and hand the keyboard back to the control that opened the list —
   * Escape from inside it would otherwise strand focus on a removed node. */
  protected dismiss(): void {
    if (!this.open()) return;
    this.open.set(false);
    this.trigger().nativeElement.focus();
  }

  /** A choice navigates; the destination decides where focus lands, so this
   * only closes the list. */
  protected onChoice(): void {
    this.open.set(false);
  }

  protected onDocumentClick(event: Event): void {
    // Every click in the app reaches this handler under zoneless CD, so a
    // closed list must bail before touching the signal — otherwise every
    // click anywhere marks this component dirty for no reason.
    if (!this.open()) return;
    const target = event.target;
    if (target instanceof Node && this.host.nativeElement.contains(target)) return;
    this.open.set(false);
  }
}
