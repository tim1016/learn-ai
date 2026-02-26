import { Component, ChangeDetectionStrategy } from '@angular/core';
import { FeatureRunnerComponent } from './feature-runner/feature-runner.component';
import { InfoPanelComponent } from './info-panel/info-panel.component';
import { ExperimentHistoryComponent } from './experiment-history/experiment-history.component';
import { TabsModule } from 'primeng/tabs';

@Component({
  selector: 'app-research-lab',
  standalone: true,
  imports: [
    FeatureRunnerComponent,
    InfoPanelComponent,
    ExperimentHistoryComponent,
    TabsModule,
  ],
  templateUrl: './research-lab.component.html',
  styleUrls: ['./research-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ResearchLabComponent {}
