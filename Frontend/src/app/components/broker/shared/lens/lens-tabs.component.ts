import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  input,
  output,
  viewChildren,
} from '@angular/core';

import { DESK_LENSES, lensFromKey, lensLabel, type DeskLens } from './lens';

/**
 * The one WAI-ARIA tablist for the Trader/Operator lens (task 2026-09-12,
 * acceptance 1). Serves the Alpaca account desk, the full bot panel, and the
 * triage detail.
 *
 * Roving tabindex: only the active tab is tabbable; ArrowLeft/ArrowRight,
 * Home, and End move both the selection and focus. Enter/Space activate the
 * focused tab through native button semantics. The component owns no lens
 * state — it renders the host's lens and reports changes — so the triage
 * detail can keep its purely local lens while the routed hosts persist
 * through `LensPreferenceService`.
 *
 * Hosts supply stable DOM ids: `idPrefix` builds `${prefix}-${lens}-tab` and
 * `${prefix}-${lens}-panel` (an empty prefix drops the leading dash). A host
 * whose two tabs swap one shared panel — the triage detail — passes
 * `panelId` instead and both tabs control that single element.
 */
@Component({
  selector: 'app-lens-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './lens-tabs.component.html',
  styleUrl: './lens-tabs.component.scss',
  host: { class: 'block' },
})
export class LensTabsComponent {
  readonly lens = input.required<DeskLens>();
  /** Stable id prefix for this host's tab and panel elements. */
  readonly idPrefix = input('');
  /** Both tabs control this one panel instead of per-lens panels. */
  readonly panelId = input<string | null>(null);
  readonly ariaLabel = input('Desk perspective');

  readonly lensChange = output<DeskLens>();

  protected readonly lenses = DESK_LENSES;

  private readonly tabs = viewChildren<ElementRef<HTMLButtonElement>>('tab');

  protected label(lens: DeskLens): string {
    return lensLabel(lens);
  }

  protected isActive(lens: DeskLens): boolean {
    return this.lens() === lens;
  }

  protected tabId(lens: DeskLens): string {
    const prefix = this.idPrefix();
    return prefix === '' ? `${lens}-tab` : `${prefix}-${lens}-tab`;
  }

  protected controls(lens: DeskLens): string {
    const shared = this.panelId();
    if (shared !== null) return shared;
    const prefix = this.idPrefix();
    return prefix === '' ? `${lens}-panel` : `${prefix}-${lens}-panel`;
  }

  protected select(lens: DeskLens): void {
    if (lens !== this.lens()) this.lensChange.emit(lens);
  }

  protected onKeydown(event: KeyboardEvent): void {
    const next = lensFromKey(event.key);
    if (next === null) return;
    event.preventDefault();
    this.select(next);
    const index = DESK_LENSES.indexOf(next);
    this.tabs()[index]?.nativeElement.focus();
  }
}
