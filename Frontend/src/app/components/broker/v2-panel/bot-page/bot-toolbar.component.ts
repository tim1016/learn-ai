import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, Directive, ElementRef, computed, inject, input, output, signal, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { AccountWorkspaceLink } from '../../../../fleet/account-workspace';
import type { ManualOrderTicketNavigation } from '../../lib/manual-order-navigation';
import type {
  BotPanelView,
  PanelAction,
  PanelActionTrigger,
  ToolbarActionView,
} from '../lib/broker-v2-panel.types';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';

type ToolbarGroupKey = ToolbarActionView['group'];
type ToolbarActionId = ToolbarActionView['action_id'];

interface ToolbarGroup {
  readonly key: ToolbarGroupKey;
  readonly label: string;
  readonly entries: readonly ToolbarActionView[];
}

/** One action as the bar or More draws it, and whether its name shows. */
interface ActionControlContext {
  readonly $implicit: ToolbarActionView;
  readonly labelled: boolean;
}

/** Types the toolbar's one action template, which the bar and More share, so it is checked like the rest. */
@Directive({ selector: 'ng-template[appBotToolbarAction]' })
export class BotToolbarActionTemplateDirective {
  static ngTemplateContextGuard(_directive: BotToolbarActionTemplateDirective, context: unknown): context is ActionControlContext {
    return context !== null && typeof context === 'object';
  }
}

const GROUP_LABELS: Readonly<Record<ToolbarGroupKey, string>> = { bot: 'Bot', fix: 'Fix', inspect: 'Inspect' };

/** Each action's icon (PrimeIcons); the backend names the action, the page draws it. */
const ICONS: Readonly<Record<ToolbarActionId, string>> = {
  stop_bot_decisions: 'pi-stop-circle',
  prepare_safe_flatten: 'pi-dollar',
  change_end: 'pi-clock',
  deploy_again: 'pi-refresh',
  archive: 'pi-inbox',
  manual_order: 'pi-pencil',
  reconcile_now: 'pi-sync',
  cancel_verified_working_orders: 'pi-times-circle',
  discharge_attributed_residue: 'pi-eraser',
  recover_exact_execution_evidence: 'pi-download',
  resolve_execution_coverage: 'pi-check-circle',
  open_custody_timeline: 'pi-history',
  build_proof: 'pi-shield',
};

/** Money moves keep their words, whatever the Labels setting (#2794 story 15). */
const ALWAYS_LABELLED: ReadonlySet<ToolbarActionId> = new Set(['stop_bot_decisions', 'prepare_safe_flatten']);

/** Bot's and Fix's actions sit on the bar; Inspect's wait under More. A
 * place follows the action's group alone, never which other actions are
 * offered, so a poll cannot move a button -- and a confirmation open on it --
 * between the bar and More. */
const BAR_GROUPS: readonly ToolbarGroupKey[] = ['bot', 'fix'];
const MORE_GROUP: ToolbarGroupKey = 'inspect';

const AVAILABILITY_WORDS: Readonly<Record<ToolbarActionView['availability'], string>> = {
  available: 'Available',
  blocked: 'Blocked',
  not_needed: 'Not needed',
};

const LABELS_KEY = 'bot-page.toolbar.labels.v1';

/** The custody actions a panel without `bot_page` still offers, in the toolbar's order and groups. */
const LEGACY_GROUPS: Readonly<Partial<Record<ToolbarActionId, ToolbarGroupKey>>> = {
  stop_bot_decisions: 'bot',
  prepare_safe_flatten: 'bot',
  reconcile_now: 'fix',
  cancel_verified_working_orders: 'fix',
  discharge_attributed_residue: 'fix',
  recover_exact_execution_evidence: 'fix',
  resolve_execution_coverage: 'fix',
  open_custody_timeline: 'inspect',
};

function isToolbarActionId(actionId: string): actionId is ToolbarActionId {
  return actionId in ICONS;
}

/**
 * The toolbar for a panel from a data plane that predates `bot_page` (a
 * rolling deploy): the panel's own custody actions with their own labels
 * and reasons, its primary, Change end and Deploy again. Nothing the old
 * page offered goes missing while the producer catches up.
 */
function legacyToolbar(panel: BotPanelView): ToolbarActionView[] {
  const entries: ToolbarActionView[] = [];
  for (const action of panel.actions) {
    const group = isToolbarActionId(action.action_id) ? LEGACY_GROUPS[action.action_id] : undefined;
    if (group === undefined || !isToolbarActionId(action.action_id)) continue;
    entries.push({
      action_id: action.action_id,
      label: action.label,
      group,
      availability: action.enabled ? 'available' : 'blocked',
      reason: action.explanation,
      tone: action.action_id === 'stop_bot_decisions' || action.action_id === 'prepare_safe_flatten' ? 'danger' : 'neutral',
      primary: action.action_id === panel.primary_action,
    });
  }
  if (panel.end?.editable) {
    entries.push({
      action_id: 'change_end', label: 'Change end', group: 'bot', availability: 'available',
      reason: panel.end.explanation, tone: 'neutral', primary: false,
    });
  }
  if (!panel.health.running) {
    entries.push({
      action_id: 'deploy_again', label: 'Deploy again', group: 'bot', availability: 'available',
      reason: 'Review a fresh deployment of this bot.', tone: 'neutral', primary: false,
    });
  }
  return entries;
}

function storedLabels(): boolean {
  try {
    return globalThis.localStorage?.getItem(LABELS_KEY) === 'on';
  } catch {
    // Storage blocked (private window, policy): the icons stay unlabelled.
    return false;
  }
}

/**
 * The bot page's toolbar (#2794 R2, R6), in the account workspace's header:
 * every action the owner can take on this bot right now, as icon buttons in
 * untitled groups -- Bot, Fix -- in the backend's order, with its plain
 * names, tones and primary. Inspect's actions wait under More, named, with
 * All actions -- every action, its availability, the backend's reason and its
 * system name -- and the Labels setting, which names every icon. Stop and Sell
 * are always named.
 *
 * An action the backend says is not needed stays out of the row; a needed
 * one it blocks shows disabled with its reason.
 *
 * Custody actions run through the shared action button, so their
 * confirmations and blockers are the ones the backend presented; Deploy
 * again and Manual order are links; Change end and Build proof open the
 * host's panels.
 */
@Component({
  selector: 'app-bot-toolbar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotToolbarActionTemplateDirective, NgTemplateOutlet, PanelActionButtonComponent, RouterLink],
  templateUrl: './bot-toolbar.component.html',
  styleUrl: './bot-toolbar.component.scss',
  host: {
    '(document:mousedown)': 'onDocumentMousedown($event)',
    '(keydown.escape)': 'onEscape()',
  },
})
export class BotToolbarComponent {
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  readonly panel = input.required<BotPanelView>();
  readonly pending = input(false);
  readonly deployAgain = input.required<AccountWorkspaceLink>();
  readonly manualOrder = input<ManualOrderTicketNavigation | null>(null);

  readonly actionRequested = output<PanelActionTrigger>();
  readonly changeEnd = output();
  readonly buildProof = output();

  protected readonly labels = signal(storedLabels());
  protected readonly moreOpen = signal(false);
  protected readonly allActionsOpen = signal(false);
  protected readonly icons = ICONS;
  /** A blocker's cure is a toolbar button of its own (Check against Alpaca, Sell), never a second one beside it. */
  protected readonly noMoves = (): boolean => false;
  protected readonly availabilityWords = AVAILABILITY_WORDS;

  protected readonly entries = computed(() => this.panel().bot_page?.toolbar ?? legacyToolbar(this.panel()));
  private readonly offered = computed(() => this.entries().filter((entry) => entry.availability !== 'not_needed'));

  protected readonly groups = computed((): readonly ToolbarGroup[] =>
    BAR_GROUPS
      .map((key) => ({
        key,
        label: GROUP_LABELS[key],
        entries: this.offered().filter((entry) => entry.group === key),
      }))
      .filter((group) => group.entries.length > 0),
  );

  /** Inspect's offered actions, named, under More. */
  protected readonly more = computed(() => this.offered().filter((entry) => entry.group === MORE_GROUP));

  /** More's name says how many actions wait in it, not only its icon's count. */
  protected readonly moreLabel = computed(() => {
    const count = this.more().length;
    return count === 0 ? 'More actions' : `More actions, ${count} not on the bar`;
  });

  private readonly moreToggle = viewChild.required<ElementRef<HTMLButtonElement>>('moreToggle');

  private readonly actionsById = computed(
    () => new Map(this.panel().actions.map((action) => [action.action_id as string, action])),
  );

  protected panelAction(entry: ToolbarActionView): PanelAction | null {
    return this.actionsById().get(entry.action_id) ?? null;
  }

  protected showLabel(entry: ToolbarActionView): boolean {
    return this.labels() || entry.primary || ALWAYS_LABELLED.has(entry.action_id);
  }

  protected opensHostPanel(entry: ToolbarActionView): boolean {
    return entry.action_id === 'change_end' || entry.action_id === 'build_proof';
  }

  /** Where Deploy again or Manual order goes; any other entry has no page of its own. */
  protected linkFor(entry: ToolbarActionView): AccountWorkspaceLink | ManualOrderTicketNavigation | null {
    if (entry.action_id === 'deploy_again') return this.deployAgain();
    if (entry.action_id === 'manual_order') return this.manualOrder();
    return null;
  }

  /** Change end and Build proof open the host's panels; More closes behind them. */
  protected open(entry: ToolbarActionView): void {
    if (entry.availability !== 'available') return;
    this.moreOpen.set(false);
    if (entry.action_id === 'change_end') this.changeEnd.emit();
    else if (entry.action_id === 'build_proof') this.buildProof.emit();
  }

  /** An action taken closes More; one that asks to confirm has done so by now. */
  protected onTriggered(trigger: PanelActionTrigger): void {
    this.moreOpen.set(false);
    this.actionRequested.emit(trigger);
  }

  /** More and All actions are never open together: one would cover the other's confirmations. */
  protected toggleMore(): void {
    this.allActionsOpen.set(false);
    this.moreOpen.set(!this.moreOpen());
  }

  protected openAllActions(): void {
    this.moreOpen.set(false);
    this.allActionsOpen.set(true);
  }

  protected closeMenus(): void {
    this.moreOpen.set(false);
    this.allActionsOpen.set(false);
  }

  /** Escape closes More or All actions and puts the keyboard back on More. */
  protected onEscape(): void {
    if (!this.moreOpen() && !this.allActionsOpen()) return;
    this.closeMenus();
    this.moreToggle().nativeElement.focus();
  }

  /** A press outside the toolbar closes More and All actions; mousedown, so one press cannot both close and reopen one. */
  protected onDocumentMousedown(event: MouseEvent): void {
    if (!this.moreOpen() && !this.allActionsOpen()) return;
    if (event.target instanceof Node && this.host.nativeElement.contains(event.target)) return;
    this.closeMenus();
  }

  protected onLabelsChange(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.toggleLabels(event.target.checked);
  }

  private toggleLabels(on: boolean): void {
    this.labels.set(on);
    try {
      globalThis.localStorage?.setItem(LABELS_KEY, on ? 'on' : 'off');
    } catch {
      // Storage blocked: the setting lasts for this page only.
    }
  }
}
