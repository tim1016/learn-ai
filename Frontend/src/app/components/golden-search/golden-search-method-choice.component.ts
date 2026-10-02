import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { GoldenSearchMethod } from './golden-search.types';

interface MethodOption {
  readonly id: GoldenSearchMethod;
  readonly title: string;
}

const METHODS: readonly MethodOption[] = [
  { id: 'zoom', title: 'Zoom search' },
  { id: 'grid', title: 'Grid search' },
];

/** What each method does, and what it cannot claim, in plain language. */
export const METHOD_HINTS: Readonly<Record<GoldenSearchMethod, string>> = {
  zoom: 'Zoom refines one knob at a time from the starting point, keeping a move only when it strictly improves the objective. It can miss settings that only work when two knobs change together, and never claims a global best.',
  grid: 'Grid tries every combination of the listed values, so the work grows as the product of the Values column. Keep it to a few varied knobs.',
};

/** Zoom or Grid as one segmented choice (#2696); the plan form shows the chosen method's hint. */
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
