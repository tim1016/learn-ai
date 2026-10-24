import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { HELP_DOCUMENTS, helpDocumentUrl, type HelpDocumentRoute } from './help-documents';

/**
 * A page's way to the document that explains it.
 *
 * It opens the document in a new browser tab. Most pages keep their selected
 * symbol, dates and unsubmitted inputs in memory, not in the URL, so leaving in
 * the same tab would lose them; a new tab leaves the page exactly as it was and
 * closing it is the way back. A page whose whole state is in its URL could open
 * the document in place instead — none does today.
 *
 * Place it in the page's header. `route` is one of the served documents, so a
 * misspelled address does not compile.
 */
@Component({
  selector: 'app-how-this-works',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <a
      class="how-this-works"
      [href]="href()"
      target="_blank"
      rel="noopener"
      [attr.title]="'Opens ' + documentTitle() + ' in a new tab'"
    >
      <i class="pi pi-question-circle" aria-hidden="true"></i>
      <span>{{ label() }}</span>
    </a>
  `,
  styleUrl: './how-this-works-link.component.scss',
})
export class HowThisWorksLinkComponent {
  /** The served document that explains the page. */
  readonly route = input.required<HelpDocumentRoute>();
  /** A section of that document, as the page's own deep links name it. */
  readonly anchor = input<string | undefined>(undefined);
  /** Query state the document reads, such as the study a manual returns to. */
  readonly query = input<Readonly<Record<string, string | undefined>>>({});
  /** Words for the link when a page offers more than one document. */
  readonly label = input('How this works');

  protected readonly href = computed(() =>
    helpDocumentUrl(this.route(), { anchor: this.anchor(), query: this.query() }),
  );
  protected readonly documentTitle = computed(
    () => HELP_DOCUMENTS.find((document) => document.route === this.route())?.title ?? this.route(),
  );
}
