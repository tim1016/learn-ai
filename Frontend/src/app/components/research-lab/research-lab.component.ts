import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FeatureRunnerComponent } from './feature-runner/feature-runner.component';
import { InfoPanelComponent } from './info-panel/info-panel.component';
import { ExperimentHistoryComponent } from './experiment-history/experiment-history.component';
import { SignalRunnerComponent } from './signal-runner/signal-runner.component';
import { SignalInfoPanelComponent } from './signal-info-panel/signal-info-panel.component';
import { SignalHistoryComponent } from './signal-history/signal-history.component';
import { BatchRunnerComponent } from './batch-runner/batch-runner.component';
import { OptionsMathDocsComponent } from './options-math-docs/options-math-docs.component';
import { DataDivergenceComponent } from './data-divergence/data-divergence.component';
import { StrategyPreflightComponent } from './strategy-preflight/strategy-preflight.component';
import { IndicatorReliabilityComponent } from './indicator-reliability/indicator-reliability.component';

type TabId =
  | 'feature-runner'
  | 'indicator-reliability'
  | 'signal-engine'
  | 'cross-sectional'
  | 'data-divergence'
  | 'strategy-preflight'
  | 'experiment-history'
  | 'options-math'
  | 'signal-docs'
  | 'signal-history'
  | 'documentation';

interface SubNavItem {
  id: TabId;
  label: string;
}

interface SubNavGroup {
  label: string;
  items: SubNavItem[];
}

@Component({
  selector: 'app-research-lab',
  imports: [
    CommonModule,
    FeatureRunnerComponent,
    InfoPanelComponent,
    ExperimentHistoryComponent,
    SignalRunnerComponent,
    SignalInfoPanelComponent,
    SignalHistoryComponent,
    BatchRunnerComponent,
    OptionsMathDocsComponent,
    DataDivergenceComponent,
    StrategyPreflightComponent,
    IndicatorReliabilityComponent,
  ],
  templateUrl: './research-lab.component.html',
  styleUrls: ['./research-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ResearchLabComponent {
  /**
   * Visual grouping of the 11 sub-pages into three meta-sections.
   * Matches the SubNav pattern from the Claude Design bundle
   * (quant-trading-lab-design-system/project/research_lab_redesign/shared/header.jsx).
   */
  readonly groups: SubNavGroup[] = [
    {
      label: 'Validate',
      items: [
        { id: 'feature-runner', label: 'Feature Runner' },
        { id: 'indicator-reliability', label: 'Indicator Reliability' },
        { id: 'signal-engine', label: 'Signal Engine' },
      ],
    },
    {
      label: 'Inspect',
      items: [
        { id: 'cross-sectional', label: 'Cross-Sectional' },
        { id: 'data-divergence', label: 'Data Divergence' },
        { id: 'strategy-preflight', label: 'Pre-flight Check' },
      ],
    },
    {
      label: 'Reference',
      items: [
        { id: 'experiment-history', label: 'Experiments' },
        { id: 'options-math', label: 'Options Math' },
        { id: 'signal-docs', label: 'Signal Docs' },
        { id: 'signal-history', label: 'Signal History' },
        { id: 'documentation', label: 'Feature Docs' },
      ],
    },
  ];

  /**
   * Indicator Reliability is the showcase page — lands here by default to
   * match the design-bundle intent. Users can switch with the sub-nav.
   */
  readonly active = signal<TabId>('indicator-reliability');

  setActive(id: TabId): void {
    this.active.set(id);
  }
}
