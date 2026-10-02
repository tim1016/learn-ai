import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { AccountWorkspaceLink } from '../../../../fleet/account-workspace';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
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
 * The bot page's toolbar (#2794 R2, R6): every action the owner can take on
 * this bot right now, as icon buttons in three groups -- Bot, Fix, Inspect --
 * in the backend's order, with its plain names, tones and primary.
 *
 * An action the backend says is not needed stays out of the row; a needed
 * one it blocks shows disabled with its reason. All actions lists every
 * action, its availability, the backend's reason and its system name. The
 * Labels setting names every icon; Stop and Sell are always named.
 *
 * Custody actions run through the shared action button, so their
 * confirmations and blockers are the ones the backend presented; Deploy
 * again and Manual order are links; Change end and Build proof open the
 * host's panels.
 */
@Component({
  selector: 'app-bot-toolbar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PanelActionButtonComponent, RouterLink, TimestampDisplayComponent],
  templateUrl: './bot-toolbar.component.html',
  styleUrl: './bot-toolbar.component.scss',
})
export class BotToolbarComponent {
  readonly panel = input.required<BotPanelView>();
  readonly pending = input(false);
  readonly deployAgain = input.required<AccountWorkspaceLink>();
  readonly manualOrder = input<ManualOrderTicketNavigation | null>(null);

  readonly actionRequested = output<PanelActionTrigger>();
  readonly changeEnd = output();
  readonly buildProof = output();

  protected readonly labels = signal(storedLabels());
  protected readonly allActionsOpen = signal(false);
  protected readonly icons = ICONS;
  /** A blocker's cure is a toolbar button of its own (Check against Alpaca, Sell), never a second one beside it. */
  protected readonly noMoves = (): boolean => false;
  protected readonly availabilityWords = AVAILABILITY_WORDS;

  protected readonly entries = computed(() => this.panel().bot_page?.toolbar ?? legacyToolbar(this.panel()));
  protected readonly groups = computed((): readonly ToolbarGroup[] =>
    (['bot', 'fix', 'inspect'] as const)
      .map((key) => ({
        key,
        label: GROUP_LABELS[key],
        entries: this.entries().filter((entry) => entry.group === key && entry.availability !== 'not_needed'),
      }))
      .filter((group) => group.entries.length > 0),
  );

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

  /** Change end and Build proof open the host's panels. */
  protected open(entry: ToolbarActionView): void {
    if (entry.availability !== 'available') return;
    if (entry.action_id === 'change_end') this.changeEnd.emit();
    else if (entry.action_id === 'build_proof') this.buildProof.emit();
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
