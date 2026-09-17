import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { inject } from '@angular/core';
import { map } from 'rxjs/operators';
import { MarkdownViewerComponent } from '../../shared/markdown-viewer/markdown-viewer.component';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';

/**
 * Full-page view of the methodology document. Reached via
 * `/docs/indicator-reliability-methodology`. URL fragment (`#section-id`)
 * deep-links into a specific section.
 */
@Component({
  selector: 'app-methodology-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MarkdownViewerComponent, PageHeaderComponent],
  template: `
    <div class="methodology-page">
      <app-page-header
        title="Indicator Reliability — Methodology"
      >
        <a
          slot="actions"
          class="page-link"
          href="/assets/docs/indicator-reliability-methodology.md"
          target="_blank"
          rel="noopener"
        >
          Raw markdown <i class="pi pi-external-link" aria-hidden="true"></i>
        </a>
      </app-page-header>

      <app-markdown-viewer
        [src]="src()"
        [scrollTo]="fragment()"
      />
    </div>
  `,
  styles: [`
      :host {
        display: block;
      }

      .methodology-page {
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

      &:hover {
        background: var(--bg-hover);
      }
    }

    .mono {
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      font-variant-numeric: tabular-nums;
    }
  `],
})
export class MethodologyPageComponent {
  private route = inject(ActivatedRoute);

  readonly src = signal('/assets/docs/indicator-reliability-methodology.md');

  /** Derived from the URL fragment (`#section-id`) for deep-linking. */
  readonly fragment = toSignal(
    this.route.fragment.pipe(map(f => f ?? null)),
    { initialValue: null },
  );
}
