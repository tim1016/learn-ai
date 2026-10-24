import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from "@angular/core";

import { CopyButtonComponent } from "../../../shared/copy-button/copy-button.component";
import { LeanSourceService } from "../../../services/lean-source.service";

/**
 * The registered LEAN validation twin — what Strategy Lab executes — beside
 * the vendored QuantConnect audit copy, which is what a port was validated
 * against. Most registered strategies have no twin, so "none registered" is a
 * first-class state here, not an error.
 */
@Component({
  selector: "app-lean-twin-source",
  imports: [CopyButtonComponent],
  templateUrl: "./lean-twin-source.component.html",
  styleUrl: "./lean-twin-source.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeanTwinSourceComponent {
  private readonly leanSource = inject(LeanSourceService);

  readonly strategyKey = input.required<string>();

  protected readonly sourceResource = resource({
    params: () => this.strategyKey(),
    loader: ({ params }) => this.leanSource.getStrategySource(params),
  });
  protected readonly result = computed(() =>
    this.sourceResource.hasValue() ? this.sourceResource.value() : null,
  );
}
