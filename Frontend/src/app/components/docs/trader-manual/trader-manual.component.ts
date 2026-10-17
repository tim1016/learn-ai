import { ChangeDetectionStrategy, Component, computed, inject, input, signal, viewChild } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { MarkdownViewerComponent } from '../../../shared/markdown-viewer/markdown-viewer.component';
import { ManualContentsComponent } from './manual-contents.component';
import { TRADER_MANUALS } from './trader-manual';

@Component({
  selector: 'app-trader-manual',
  imports: [RouterLink, MarkdownViewerComponent, ManualContentsComponent],
  templateUrl: './trader-manual.component.html',
  styleUrl: './trader-manual.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TraderManualComponent {
  readonly manual = input.required<'options' | 'broker'>();
  private readonly route = inject(ActivatedRoute);
  private readonly viewer = viewChild(MarkdownViewerComponent);
  protected readonly book = computed(() => TRADER_MANUALS[this.manual()]);
  protected readonly contentsOpen = signal(false);
  protected readonly fragment = toSignal(this.route.fragment, { initialValue: null });
  protected readonly canPrint = computed(() => {
    const viewer = this.viewer();
    return !!viewer && !viewer.loading() && !viewer.error() && !!viewer.rendered();
  });

  protected print(): void {
    if (this.canPrint()) window.print();
  }
}
