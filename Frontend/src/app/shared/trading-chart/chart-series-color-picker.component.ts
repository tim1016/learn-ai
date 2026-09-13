import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  input,
  output,
  viewChildren,
} from '@angular/core';

import {
  CHART_SERIES_COLOR_TOKENS,
  CHART_SERIES_ELIGIBLE_TOKENS,
  ChartSeriesColorToken,
} from './chart-series-color-tokens';

/** Accessible named swatch radio group for assigning a chart series color
 *  token (PRD §10). Emits token IDs only — never hex or arbitrary CSS. The
 *  selected swatch carries a checked outline plus an aria-checked radio role,
 *  so selection is never signaled by color alone. */
@Component({
  selector: 'app-chart-series-color-picker',
  templateUrl: './chart-series-color-picker.component.html',
  styleUrl: './chart-series-color-picker.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'cscp-root' },
})
export class ChartSeriesColorPickerComponent {
  /** Currently selected token, or null for no selection. */
  readonly selected = input<ChartSeriesColorToken | null>(null);
  /** Accessible name for the group (e.g. "EMA (length=20) series color"). */
  readonly groupLabel = input('Chart series color');

  readonly tokenSelected = output<ChartSeriesColorToken>();

  protected readonly tokens = CHART_SERIES_ELIGIBLE_TOKENS;

  private readonly defs = CHART_SERIES_COLOR_TOKENS;
  private readonly radios =
    viewChildren<ElementRef<HTMLButtonElement>>('swatchRadio');

  protected swatchFor(token: ChartSeriesColorToken): string {
    return `var(${this.defs.get(token)?.cssVar ?? '--chart-series-blue'})`;
  }

  protected labelFor(token: ChartSeriesColorToken): string {
    return this.defs.get(token)?.label ?? token;
  }

  protected isSelected(token: ChartSeriesColorToken): boolean {
    return this.selected() === token;
  }

  protected select(token: ChartSeriesColorToken): void {
    this.tokenSelected.emit(token);
  }

  /** Focus is kept on one roving radio; arrows move between options. */
  protected onRadioKeydown(event: KeyboardEvent, index: number): void {
    const count = this.tokens.length;
    let next: number | null = null;
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        next = (index + 1) % count;
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        next = (index - 1 + count) % count;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = count - 1;
        break;
    }
    if (next !== null) {
      event.preventDefault();
      this.radios()[next]?.nativeElement.focus();
    }
  }

  /** Roving tabindex — the selected option (or the first when nothing is
   *  selected) is the group's single tab stop. */
  protected tabIndexFor(token: ChartSeriesColorToken): number {
    const sel = this.selected();
    if (sel !== null) return sel === token ? 0 : -1;
    return this.tokens[0] === token ? 0 : -1;
  }
}
