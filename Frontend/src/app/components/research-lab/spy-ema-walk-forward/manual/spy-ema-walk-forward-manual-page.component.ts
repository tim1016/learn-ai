import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { map } from 'rxjs/operators';

import { MarkdownViewerComponent } from '../../../../shared/markdown-viewer/markdown-viewer.component';
import { PageHeaderComponent } from '../../../../shared/page-header/page-header.component';

/** Internal field manual for operating and extending the frozen SPY EMA protocol. */
@Component({
  selector: 'app-spy-ema-walk-forward-manual-page',
  imports: [MarkdownViewerComponent, PageHeaderComponent, RouterLink],
  templateUrl: './spy-ema-walk-forward-manual-page.component.html',
  styleUrl: './spy-ema-walk-forward-manual-page.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SpyEmaWalkForwardManualPageComponent {
  private readonly route = inject(ActivatedRoute);

  readonly src = '/assets/docs/spy-ema-walk-forward-user-manual.md';
  readonly fragment = toSignal(
    this.route.fragment.pipe(map(fragment => fragment ?? null)),
    { initialValue: null },
  );
}
