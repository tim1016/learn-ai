import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { GoldenSearchGridComponent } from '../charts/golden-search-grid.component';
import { MANUAL_STEPS } from './golden-search-manual';

@Component({
  selector: 'app-golden-search-manual',
  imports: [RouterLink, PageHeaderComponent, GoldenSearchGridComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-manual.component.html',
  styleUrl: './golden-search-manual.component.scss',
})
export class GoldenSearchManualComponent {
  readonly study = input<string | undefined>(undefined);
  protected readonly steps = MANUAL_STEPS;
  protected readonly studyRoute = computed(() => this.study() ? ['/golden-search', this.study()] : ['/golden-search']);
}
