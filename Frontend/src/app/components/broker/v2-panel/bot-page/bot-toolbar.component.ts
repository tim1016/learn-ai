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
import { PanelActionButtonComponent, type PanelActionTone } from '../panel-action-button/panel-action-button.component';

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
  protected readonly availabilityWords = AVAILABILITY_WORDS;

  protected readonly entries = computed(() => this.panel().bot_page?.toolbar ?? []);
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

  protected tone(entry: ToolbarActionView): PanelActionTone {
    return entry.primary ? 'primary' : entry.tone;
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
