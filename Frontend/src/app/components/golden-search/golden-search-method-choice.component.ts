import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { GoldenSearchMethod } from './golden-search.types';

interface MethodOption {
  readonly id: GoldenSearchMethod;
  readonly title: string;
  readonly question: string;
  readonly limitation: string;
}

const METHODS: readonly MethodOption[] = [
  {
    id: 'zoom',
    title: 'Zoom Search',
    question: 'Refine one knob at a time, keeping a move only when it strictly improves the objective. Useful for many controls.',
    limitation: 'Local and order-dependent: it can miss settings that only work when two knobs change together. It never claims a global best.',
  },
  {
    id: 'grid',
    title: 'Grid Search',
    question: 'Try every listed combination. Best for small domains or a focused two-knob comparison.',
    limitation: 'Tests only the listed values, and the work grows as their product. Keep it to a few searched knobs.',
  },
];

/** Zoom or Grid in plain language, each with its limitation (#2696). */
@Component({
  selector: 'app-golden-search-method-choice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-method-choice.component.html',
  styleUrl: './golden-search-method-choice.component.scss',
})
export class GoldenSearchMethodChoiceComponent {
  readonly method = input.required<GoldenSearchMethod>();
  readonly readonly = input(false);
  readonly methodChange = output<GoldenSearchMethod>();

  protected readonly methods = METHODS;
}
