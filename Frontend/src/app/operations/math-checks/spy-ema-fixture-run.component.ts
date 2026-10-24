import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';

import { StrategyRunsService } from '../../services/strategy-runs.service';
import { errorMessage } from '../../shared/errors/error-message';

/**
 * Operations → Math checks: the explicit "Run SPY EMA fixture" tool.
 *
 * It POSTs the canonical fixture spec verbatim and persists a run, which
 * exercises the whole reproducibility contract end to end. Nothing here starts
 * on its own: a run happens only when the button is pressed. The run it makes
 * is an ordinary strategy run, so its detail stays where every strategy run's
 * does, in Research.
 */
@Component({
  selector: 'app-spy-ema-fixture-run',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ButtonModule, MessageModule, RouterLink],
  templateUrl: './spy-ema-fixture-run.component.html',
  styleUrl: './spy-ema-fixture-run.component.scss',
})
export class SpyEmaFixtureRunComponent {
  private readonly service = inject(StrategyRunsService);

  protected readonly running = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly lastRunId = signal<string | null>(null);

  protected async run(): Promise<void> {
    this.running.set(true);
    this.error.set(null);
    try {
      const { ledger } = await this.service.runSpyEmaFixture();
      this.lastRunId.set(ledger.run_id);
    } catch (err) {
      this.error.set(errorMessage(err));
    } finally {
      this.running.set(false);
    }
  }

  protected shortId(runId: string): string {
    return `${runId.slice(0, 12)}…`;
  }
}
