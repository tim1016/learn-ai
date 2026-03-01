import { Component, ChangeDetectionStrategy } from '@angular/core';
import { FeatureRunnerComponent } from './feature-runner/feature-runner.component';
import { InfoPanelComponent } from './info-panel/info-panel.component';
import { ExperimentHistoryComponent } from './experiment-history/experiment-history.component';
import { SignalRunnerComponent } from './signal-runner/signal-runner.component';
import { SignalInfoPanelComponent } from './signal-info-panel/signal-info-panel.component';
import { SignalHistoryComponent } from './signal-history/signal-history.component';
import { BatchRunnerComponent } from './batch-runner/batch-runner.component';
import { TabsModule } from 'primeng/tabs';

@Component({
  selector: 'app-research-lab',
  standalone: true,
  imports: [
    FeatureRunnerComponent,
    InfoPanelComponent,
    ExperimentHistoryComponent,
    SignalRunnerComponent,
    SignalInfoPanelComponent,
    SignalHistoryComponent,
    BatchRunnerComponent,
    TabsModule,
  ],
  templateUrl: './research-lab.component.html',
  styleUrls: ['./research-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ResearchLabComponent {}
