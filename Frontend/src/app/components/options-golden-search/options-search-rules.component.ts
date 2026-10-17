import { ChangeDetectionStrategy, Component, computed, effect, input, output, signal } from '@angular/core';
import { form, FormField, max, min } from '@angular/forms/signals';

import { etIsoDate } from '../../shared/date/et-midnight';
import { DateRangePickerComponent, UNBOUNDED, type DateBounds, type DateRange } from '../../shared/date-picker';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { SearchDeclarations, PlanRequest, Preflight } from './options-golden-search.types';

export interface RuleSelection {
  readonly policy: PlanRequest['policy'];
  readonly method: PlanRequest['method'];
  readonly train_months: number;
  readonly test_months: number;
  readonly final_months: number;
  readonly budget: PlanRequest['budget'];
  readonly finalist_count: number;
  readonly stress: PlanRequest['stress'];
  readonly zoom: PlanRequest['zoom'];
  readonly pair_audits: PlanRequest['pair_audits'];
}

@Component({
  selector: 'app-options-search-rules',
  imports: [DateRangePickerComponent, FormField, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-search-rules.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsSearchRulesComponent {
  readonly declarations = input.required<SearchDeclarations>();
  readonly preflight = input<Preflight | null>(null);
  readonly range = input.required<DateRange>();
  readonly rangeChanged = output<DateRange>();
  readonly changed = output<RuleSelection>();
  protected readonly model = signal({ drawdown: '20', sharpe: 1, activity: 50, observations: 30, forward: 30, final: 30, recoveryOn: false, recovery: 60, riskFree: 0, method: 'zoom', trainMonths: 6, testMonths: 2, finalMonths: 6, budget: 5000, finalists: 3, slippageStress: true, feeStress: true, stressGate: false, slippageMultiplier: '2', feeMultiplier: '2', slippageAbsolute: '', feeAbsolute: '', zoomPoints: 5, zoomRefinements: 2, zoomPasses: 2, pairFirst: '', pairSecond: '' });
  protected readonly fields = form(this.model, (path) => {
    for (const field of [path.activity, path.forward, path.final, path.recovery, path.trainMonths, path.testMonths, path.finalMonths, path.budget, path.finalists, path.zoomPasses]) min(field, 1);
    min(path.observations, 2); min(path.zoomPoints, 3); min(path.zoomRefinements, 0);
    max(path.budget, 5000); max(path.finalists, 10); max(path.zoomPoints, 9); max(path.zoomRefinements, 4); max(path.zoomPasses, 5);
  });
  protected readonly bounds = computed<DateBounds>(() => {
    const bounds = this.declarations().history_bounds;
    return { ...UNBOUNDED, min: bounds.start_ms === null ? null : etIsoDate(bounds.start_ms), max: bounds.end_ms === null ? null : etIsoDate(bounds.end_ms - 1), minReason: 'Pinned Options history begins here.', maxReason: 'Pinned Options history ends here.', unknown: bounds.start_ms === null || bounds.end_ms === null ? 'The server has not established retained history bounds. Preflight must verify coverage.' : null };
  });
  protected readonly changedPreset = computed(() => {
    const defaults = this.declarations().defaults;
    const value = this.model();
    return value.drawdown !== defaults.policy.max_drawdown_percent || value.sharpe !== defaults.policy.min_sharpe || value.activity !== defaults.policy.min_activity_per_year || value.observations !== defaults.policy.min_daily_observations || value.forward !== defaults.policy.min_forward_sessions || value.final !== defaults.policy.min_final_sessions || (value.recoveryOn ? value.recovery : null) !== defaults.policy.max_recovery_sessions || value.riskFree !== defaults.policy.risk_free_daily_return || value.stressGate !== defaults.policy.stress_gate;
  });

  constructor() {
    effect(() => {
      const defaults = this.declarations().defaults;
      this.model.update((value) => ({ ...value, drawdown: defaults.policy.max_drawdown_percent, sharpe: defaults.policy.min_sharpe, activity: defaults.policy.min_activity_per_year, observations: defaults.policy.min_daily_observations, forward: defaults.policy.min_forward_sessions, final: defaults.policy.min_final_sessions, riskFree: defaults.policy.risk_free_daily_return, recoveryOn: defaults.policy.max_recovery_sessions !== null, recovery: defaults.policy.max_recovery_sessions ?? 60, trainMonths: defaults.train_months, testMonths: defaults.test_months, finalMonths: defaults.final_months, budget: defaults.evaluation_cap, finalists: defaults.finalist_count }));
    });
    effect(() => {
      const value = this.model();
      this.changed.emit({
        method: value.method === 'grid' ? 'grid' : 'zoom',
        policy: { max_drawdown_percent: value.drawdown, min_sharpe: value.sharpe, min_activity_per_year: value.activity, min_daily_observations: value.observations, min_forward_sessions: value.forward, min_final_sessions: value.final, max_recovery_sessions: value.recoveryOn ? value.recovery : null, risk_free_daily_return: value.riskFree, stress_gate: value.stressGate },
        train_months: value.trainMonths, test_months: value.testMonths, final_months: value.finalMonths,
        budget: { evaluation_cap: value.budget }, finalist_count: value.finalists,
        stress: [...(value.slippageStress ? [{ key: 'slippage', slippage_multiplier: value.slippageMultiplier, fee_multiplier: '1', ...(value.slippageAbsolute.trim() !== '' ? { slippage_percent: value.slippageAbsolute.trim() } : {}) }] : []), ...(value.feeStress ? [{ key: 'fees', slippage_multiplier: '1', fee_multiplier: value.feeMultiplier, ...(value.feeAbsolute.trim() !== '' ? { fee_per_contract_usd: value.feeAbsolute.trim() } : {}) }] : [])],
        zoom: { points: value.zoomPoints, refinements: value.zoomRefinements, passes: value.zoomPasses },
        pair_audits: value.pairFirst !== '' && value.pairSecond !== '' ? [[value.pairFirst, value.pairSecond]] : [],
      });
    });
  }
}
