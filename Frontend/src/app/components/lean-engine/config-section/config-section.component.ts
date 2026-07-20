import { ChangeDetectionStrategy, Component, input, model } from '@angular/core';

/**
 * A single collapsible section of the Engine Lab config nav.
 *
 * Chrome only: it owns the disclosure header, the editorial section index,
 * and the collapsed one-line summary. The actual input controls are
 * projected via `<ng-content>` and stay bound to the parent's signals — so
 * folding the nav never plumbs form state through a child.
 *
 * When collapsed and `configured`, the header shows `summary` instead of the
 * body, giving the "SPY · 2024-01→06 · minute" fold the workbench uses to
 * reclaim width once a run exists.
 */
@Component({
  selector: 'app-config-section',
  templateUrl: './config-section.component.html',
  styleUrl: './config-section.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ConfigSectionComponent {
  readonly title = input.required<string>();
  /** Full PrimeIcons class, e.g. `pi pi-calendar`. */
  readonly icon = input('');
  /** Editorial index marker, e.g. `01`. */
  readonly index = input('');
  /** Whether the section's inputs hold a usable value (drives the summary). */
  readonly configured = input(false);
  /** One-line summary shown when collapsed and configured. */
  readonly summary = input('');
  /** Two-way open/closed state; the parent owns it so it can auto-collapse. */
  readonly open = model(true);

  protected toggle(): void {
    this.open.update((v) => !v);
  }
}
