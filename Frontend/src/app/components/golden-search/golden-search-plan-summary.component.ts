import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DecimalPipe, PercentPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';

import { fillModeLabel } from '../../models/fill-mode';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { incumbentLabel, knobsByName, usesTradeFrequency } from './golden-search-display';
import { GoldenSearchActivityComponent } from './golden-search-activity.component';
import { GoldenSearchKnobTableComponent } from './golden-search-knob-table.component';
import type { StrategyCapability, StudyDetail } from './golden-search.types';

/**
 * A locked study's frozen plan, read-only (#2696), with its receipt and the
 * way to change it: Revise starts a new linked study from this plan. The
 * study itself never changes.
 */
@Component({
  selector: 'app-golden-search-plan-summary',
  imports: [ButtonModule, DecimalPipe, GoldenSearchActivityComponent, GoldenSearchKnobTableComponent, PercentPipe, ReceiptLabelPipe, RouterLink, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-plan-summary.component.html',
  styleUrl: './golden-search-plan-summary.component.scss',
})
export class GoldenSearchPlanSummaryComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);

  protected readonly fillMode = computed(() => fillModeLabel(this.study().protocol.execution.fill_mode));
  protected readonly canRevise = computed(() => this.study().permitted_actions.includes('revise'));
  protected readonly frequencyBased = computed(() => usesTradeFrequency(this.study().protocol));
  protected readonly pairs = computed(() => {
    const knobs = knobsByName(this.capability());
    return this.study().protocol.pair_audits.map(([a, b]) => `${knobs.get(a)?.label ?? a} × ${knobs.get(b)?.label ?? b}`);
  });
  protected readonly incumbent = computed(() => incumbentLabel(this.study().protocol.incumbent));
}
