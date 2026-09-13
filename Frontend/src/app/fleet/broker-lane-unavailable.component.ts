import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

/** Failure state for a compatibility URL that cannot prove its exact lane.
 * It intentionally renders in place: users must choose a valid lane rather
 * than being redirected to an unrelated Alpaca account. */
@Component({
  selector: 'app-broker-lane-unavailable',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    <main aria-labelledby="lane-unavailable-title">
      <h1 id="lane-unavailable-title">Broker lane unavailable</h1>
      <p role="alert">
        This link cannot be matched to an accessible clerk and account. Its target was not changed.
      </p>
      <a routerLink="/brokers/alpaca">View the Alpaca lane directory</a>
    </main>
  `,
})
export class BrokerLaneUnavailableComponent {}
