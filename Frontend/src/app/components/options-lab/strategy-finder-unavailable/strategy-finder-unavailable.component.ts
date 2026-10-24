import { ChangeDetectionStrategy, Component } from '@angular/core';

/**
 * Why the Strategy finder page has nothing to run. The scanner it names was
 * never built and its ranking rules were never chosen, so the page says so
 * rather than looking like an unfinished tool.
 */
export const STRATEGY_FINDER_UNAVAILABLE_REASON =
  'No scanner exists behind this page, and the rules for ranking strategies '
  + '(reward to risk, breakeven distance, expected payoff across a price range) have not been decided.';

@Component({
  selector: 'app-strategy-finder-unavailable',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './strategy-finder-unavailable.component.html',
  styleUrl: './strategy-finder-unavailable.component.scss',
})
export class StrategyFinderUnavailableComponent {
  protected readonly reason = STRATEGY_FINDER_UNAVAILABLE_REASON;
}
