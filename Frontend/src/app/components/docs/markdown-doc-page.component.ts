import { ChangeDetectionStrategy, Component, inject, input } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { map } from 'rxjs/operators';
import { MarkdownViewerComponent } from '../../shared/markdown-viewer/markdown-viewer.component';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';

/**
 * Full-page view of one Markdown document served from `src/assets/docs/`.
 *
 * The route supplies `heading` and `src` as route data (component input
 * binding), and names the canonical repo document the served file copies.
 * The URL fragment (`#section-id`) deep-links into a specific section.
 */
@Component({
  selector: 'app-markdown-doc-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MarkdownViewerComponent, PageHeaderComponent],
  template: `
    <div class="doc-page">
      <app-page-header [title]="heading()">
        <a slot="actions" class="page-link" [href]="src()" target="_blank" rel="noopener">
          Raw markdown <i class="pi pi-external-link" aria-hidden="true"></i>
        </a>
      </app-page-header>

      <app-markdown-viewer [src]="src()" [scrollTo]="fragment()" />
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
      }

      .doc-page {
        max-width: 960px;
        margin: 0 auto;
      }

      .page-link {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 0.78rem;
        color: var(--accent);
        text-decoration: none;
        padding: 5px 10px;
        border: 1px solid var(--border);
        border-radius: 4px;
      }

      .page-link:hover {
        background: var(--bg-hover);
      }
    `,
  ],
})
export class MarkdownDocPageComponent {
  private readonly route = inject(ActivatedRoute);

  readonly heading = input.required<string>();
  readonly src = input.required<string>();

  readonly fragment = toSignal(this.route.fragment.pipe(map((f) => f ?? null)), {
    initialValue: null,
  });
}
