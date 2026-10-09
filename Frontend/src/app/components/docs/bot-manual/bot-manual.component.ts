import { ChangeDetectionStrategy, Component, computed, inject, viewChild } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { MarkdownViewerComponent } from '../../../shared/markdown-viewer/markdown-viewer.component';

@Component({
  selector: 'app-bot-manual',
  imports: [MarkdownViewerComponent, RouterLink],
  templateUrl: './bot-manual.component.html',
  styleUrl: './bot-manual.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BotManualComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly viewer = viewChild(MarkdownViewerComponent);
  readonly fragment = toSignal(this.route.fragment, { initialValue: null });
  readonly canPrint = computed(() => {
    const viewer = this.viewer();
    return !!viewer && !viewer.loading() && !viewer.error() && !!viewer.rendered();
  });
  readonly chapters = [
    { id: 'big-picture', label: 'The big picture' },
    { id: 'trading-worlds', label: 'Dry Run, Paper, Shadow & Live' },
    { id: 'decision-clock', label: 'How a bot makes a decision' },
    { id: 'strategy-rules', label: 'The strategy rulebook' },
    { id: 'stocks-and-budgets', label: 'Stocks, sizing & budgets' },
    { id: 'option-structures', label: 'Options & structures' },
    { id: 'picking-contracts', label: 'How contracts are picked' },
    { id: 'option-orders', label: 'Option prices, orders & fills' },
    { id: 'exits-and-stopping', label: 'Exits, Stop & Flatten' },
    { id: 'operating-routine', label: 'Your operating routine' },
    { id: 'reading-evidence', label: 'Evidence & troubleshooting' },
    { id: 'glossary', label: 'Plain-language glossary' },
    { id: 'source-notes', label: 'Implementation references' },
  ];

  print(): void {
    if (this.canPrint()) window.print();
  }
}
