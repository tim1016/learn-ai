import { ChangeDetectionStrategy, Component, ElementRef, inject, signal } from '@angular/core';

import { HELP_DOCUMENTS } from '../shared/help/help-documents';

/**
 * The top bar's "?": an index of every served document.
 *
 * Each link opens its document, at the address the Documentation menu uses,
 * in a new browser tab. The page being read keeps its selected symbol and
 * dates, which live in memory and not in the URL, so closing the tab is the
 * way back to the task.
 */
@Component({
  selector: 'app-help-index',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:mousedown)': 'onDocumentMousedown($event)',
    '(keydown.escape)': 'open.set(false)',
  },
  styleUrl: './help-index.component.scss',
  template: `
    <button
      type="button"
      class="help-button"
      aria-label="Help documents"
      aria-controls="help-index-panel"
      [attr.aria-expanded]="open()"
      (click)="open.set(!open())"
    >?</button>
    @if (open()) {
      <nav id="help-index-panel" class="panel" aria-label="Help documents">
        <ul class="panel__list">
          @for (document of documents; track document.route) {
            <li>
              <a
                class="panel__link"
                [href]="document.route"
                target="_blank"
                rel="noopener"
                (click)="open.set(false)"
              >
                <span class="panel__title">{{ document.title }}</span>
                <span class="panel__description">{{ document.description }}</span>
              </a>
            </li>
          }
        </ul>
      </nav>
    }
  `,
})
export class HelpIndexComponent {
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  protected readonly documents = HELP_DOCUMENTS;
  protected readonly open = signal(false);

  /** A press anywhere outside the index closes it; mousedown, as the shell's other popovers do, so one press cannot both close it and toggle it back open. */
  protected onDocumentMousedown(event: MouseEvent): void {
    if (!this.open()) return;
    if (event.target instanceof Node && this.host.nativeElement.contains(event.target)) return;
    this.open.set(false);
  }
}
