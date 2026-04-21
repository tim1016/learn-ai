import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { inject } from '@angular/core';
import { map } from 'rxjs/operators';
import { MarkdownViewerComponent } from '../../shared/markdown-viewer/markdown-viewer.component';

/**
 * Full-page view of the methodology document. Reached via
 * `/docs/indicator-reliability-methodology`. URL fragment (`#section-id`)
 * deep-links into a specific section.
 */
@Component({
  selector: 'app-methodology-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MarkdownViewerComponent],
  template: `
    <div class="methodology-page">
      <header class="page-header">
        <span class="page-eyebrow mono">Reference</span>
        <h1 class="page-title">Indicator Reliability — Methodology</h1>
        <p class="page-subtitle">
          Consolidated reference: IC statistics, multiple-testing correction,
          random baselines, regime conditioning, IR proxy, and the
          mission-control UI.
        </p>
        <div class="page-actions">
          <a
            class="page-link"
            href="/assets/docs/indicator-reliability-methodology.md"
            target="_blank"
            rel="noopener"
          >
            Raw markdown <i class="pi pi-external-link" aria-hidden="true"></i>
          </a>
        </div>
      </header>

      <app-markdown-viewer
        [src]="src()"
        [scrollTo]="fragment()"
      />
    </div>
  `,
  styles: [`
    :host {
      display: block;
      max-width: 960px;
      margin: 0 auto;
      padding: 4px 4px 40px;
    }

    .page-header {
      margin-bottom: 24px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border);
    }

    .page-eyebrow {
      display: inline-block;
      font-size: 0.68rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--text-muted);
      margin-bottom: 8px;
    }
    .page-title {
      margin: 0 0 8px;
      font-size: 1.75rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--text-primary);
    }
    .page-subtitle {
      margin: 0;
      color: var(--text-secondary);
      font-size: 0.92rem;
      line-height: 1.6;
      max-width: 720px;
    }
    .page-actions {
      margin-top: 14px;
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
