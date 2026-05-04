import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

export interface PageGuideRelated {
  label: string;
  route: string;
}

/**
 * Default-collapsed "How this page works" disclosure that answers
 * three questions: what data the page pulls, why a user is on it,
 * and what to do next. Intended to slot into ``app-page-header`` via
 * ``slot="guide"`` but renders standalone too.
 *
 * Pages with bespoke guide content (e.g. Market Data's existing
 * Quick start / What you'll see / Where to go next block) can keep
 * their bespoke copy — this component is for the common shape.
 */
@Component({
  selector: 'app-page-guide',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  styleUrl: './page-guide.component.scss',
  template: `
    <details class="page-guide" [attr.open]="open() ? '' : null">
      <summary class="page-guide-summary">
        <span class="page-guide-icon" aria-hidden="true">i</span>
        <span>{{ summaryLabel() }}</span>
      </summary>
      <div class="page-guide-body">
        <dl class="page-guide-meta">
          <div>
            <dt>Pulls</dt>
            <dd>{{ pulls() }}</dd>
          </div>
          <div>
            <dt>Why</dt>
            <dd>{{ why() }}</dd>
          </div>
        </dl>
        @if (steps().length > 0) {
          <div class="page-guide-section">
            <h4>What to do</h4>
            <ol>
              @for (step of steps(); track step) {
                <li>{{ step }}</li>
              }
            </ol>
          </div>
        }
        @if (related().length > 0) {
          <div class="page-guide-section">
            <h4>Related</h4>
            <ul class="page-guide-related">
              @for (link of related(); track link.route) {
                <li><a [routerLink]="link.route">{{ link.label }}</a></li>
              }
            </ul>
          </div>
        }
      </div>
    </details>
  `,
})
export class PageGuideComponent {
  readonly pulls = input.required<string>();
  readonly why = input.required<string>();
  readonly steps = input<readonly string[]>([]);
  readonly related = input<readonly PageGuideRelated[]>([]);
  readonly open = input(false);
  readonly label = input<string | undefined>(undefined);

  readonly summaryLabel = computed(() => this.label() ?? 'How this page works');
}
