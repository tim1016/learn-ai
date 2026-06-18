import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { UpperCasePipe } from '@angular/common';
import type {
  ActionPlan,
  ActionPlanEntryLeg,
  ActionPlanExitEntity,
  OptionEntryLeg,
} from '../../../../api/action-plan.types';
import { isOptionLeg } from '../../../../api/action-plan.types';

/**
 * Read-only cockpit card that surfaces the bound run's declared action
 * plan (PRD #593, ADR 0012). The plan is sourced from
 * ``ledger.live_config.action`` via ``/status``; the engine does NOT
 * consume it in Slices 1–3, so the card carries the explicit literal
 * label to keep the operator honest.
 *
 * Renders nothing when ``actionPlan`` is null — legacy / pre-Slice-1A
 * ledgers must not surface an empty card.
 *
 * Slice 1A — empty-state label.
 * Slice 1B — stock entry leg + close_leg row rendering.
 * Slice 1C — option entry leg with human-readable selector summaries
 *            ("Long call · ATM · min_dte 14d", "Short put · ATM-5 ·
 *            2026-06-25 (absolute)").
 */
@Component({
  selector: 'app-action-plan-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [UpperCasePipe],
  templateUrl: './action-plan-card.component.html',
  styleUrl: './action-plan-card.component.scss',
})
export class ActionPlanCardComponent {
  readonly actionPlan = input.required<ActionPlan | null>();

  readonly hasPlan = computed<boolean>(() => this.actionPlan() !== null);

  readonly entryLegs = computed<ActionPlanEntryLeg[]>(() => this.actionPlan()?.on_enter ?? []);

  readonly exitEntities = computed<ActionPlanExitEntity[]>(() => this.actionPlan()?.on_exit ?? []);

  readonly entryLegCount = computed<number>(() => this.entryLegs().length);

  readonly exitEntityCount = computed<number>(() => this.exitEntities().length);

  readonly isEmpty = computed<boolean>(
    () => this.hasPlan() && this.entryLegCount() === 0 && this.exitEntityCount() === 0,
  );

  isOption = isOptionLeg;

  /** Human-readable summary for an option leg used on the card.
   * Format: ``"Long call · ATM · min_dte 14d"``. Display only — the
   * stored leg is authoritative. */
  optionSummary(leg: OptionEntryLeg): string {
    const direction = leg.position === 'long' ? 'Long' : 'Short';
    return `${direction} ${leg.right} · ${formatStrike(leg)} · ${formatExpiry(leg)}`;
  }
}

function formatStrike(leg: OptionEntryLeg): string {
  const s = leg.strike;
  switch (s.selector) {
    case 'atm':
      return 'ATM';
    case 'atm_offset':
      return s.offset >= 0 ? `ATM+${s.offset}` : `ATM${s.offset}`;
  }
}

function formatExpiry(leg: OptionEntryLeg): string {
  const e = leg.expiry;
  switch (e.selector) {
    case 'min_dte':
      return `min_dte ${e.days}d`;
    case 'nearest_weekly':
      return 'nearest weekly';
    case 'absolute':
      // Per ADR 0012 / repo timestamp policy — display in America/New_York.
      // Storage and wire format remain int64 ms UTC.
      return formatNyDate(e.expiration_ms);
  }
}

function formatNyDate(ms: number): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date(ms));
  const yyyy = parts.find((p) => p.type === 'year')?.value ?? '';
  const mm = parts.find((p) => p.type === 'month')?.value ?? '';
  const dd = parts.find((p) => p.type === 'day')?.value ?? '';
  return `${yyyy}-${mm}-${dd}`;
}
