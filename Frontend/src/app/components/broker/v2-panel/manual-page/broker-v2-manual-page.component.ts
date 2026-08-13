import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { ActivatedRoute, ParamMap } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { map } from 'rxjs/operators';
import { MarkdownViewerComponent } from '../../../../shared/markdown-viewer/markdown-viewer.component';
import { PageHeaderComponent } from '../../../../shared/page-header/page-header.component';

/**
 * Route: /brokers/:broker/manual
 * Renders the broker-v2 operator manual markdown, scrolling to the fragment
 * from the URL. Pattern: follows IbkrSetupGuidePageComponent exactly.
 */
@Component({
  selector: 'app-broker-v2-manual-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MarkdownViewerComponent, PageHeaderComponent],
  template: `
    <div class="manual-page">
      <app-page-header
        eyebrow="Operator Manual"
        [title]="pageTitle()"
        subtitle="Six-station order pipeline, button reference, and closed vocabulary for the broker-v2 control panel."
      >
        <a
          slot="actions"
          class="page-link"
          href="/assets/docs/broker-v2-operator-manual.md"
          target="_blank"
          rel="noopener"
        >
          Raw markdown <i class="pi pi-external-link" aria-hidden="true"></i>
        </a>
      </app-page-header>

      <app-markdown-viewer [src]="src" [scrollTo]="fragment()" />
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
      }

      .manual-page {
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
export class BrokerV2ManualPageComponent {
  private readonly route = inject(ActivatedRoute);

  readonly src = '/assets/docs/broker-v2-operator-manual.md';

  readonly fragment = toSignal(this.route.fragment.pipe(map((f: string | null) => f ?? null)), {
    initialValue: null,
  });

  readonly broker = toSignal(
    this.route.paramMap.pipe(map((p: ParamMap) => p.get('broker') ?? 'alpaca')),
    { initialValue: 'alpaca' },
  );

  readonly pageTitle = computed(() => {
    const b = this.broker();
    return `${b.charAt(0).toUpperCase()}${b.slice(1)} — Broker V2 Manual`;
  });
}
