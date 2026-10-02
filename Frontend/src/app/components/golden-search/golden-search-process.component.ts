import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/** The research process a Golden Search study follows (#2811), from the plan to a deployable configuration. */
export const RESEARCH_PROCESS = [
  { id: 'plan', label: 'Plan' },
  { id: 'research', label: 'Research' },
  { id: 'compare', label: 'Compare' },
  { id: 'final', label: 'Final test' },
  { id: 'decision', label: 'Decision' },
  { id: 'deploy', label: 'Deploy' },
] as const;

export type ResearchStage = (typeof RESEARCH_PROCESS)[number]['id'];

/** Where the user is in the research process; finished stages are marked by text and icon, not colour alone. */
@Component({
  selector: 'app-golden-search-process',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-process.component.html',
  styleUrl: './golden-search-process.component.scss',
})
export class GoldenSearchProcessComponent {
  readonly current = input.required<ResearchStage>();

  protected readonly stages = computed(() => {
    const at = RESEARCH_PROCESS.findIndex((stage) => stage.id === this.current());
    return RESEARCH_PROCESS.map((stage, index) => ({ ...stage, state: index < at ? 'done' : index === at ? 'current' : 'upcoming' }));
  });
}
