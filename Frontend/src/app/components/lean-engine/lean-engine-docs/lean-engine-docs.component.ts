import { ChangeDetectionStrategy, Component } from "@angular/core";
import { RouterModule } from "@angular/router";

/**
 * Engine documentation page. Placeholder scaffold — full content (pipeline
 * walkthrough, indicator/statistics formulas with KaTeX, SPY first-trade
 * worked example) will be filled in as part of Phase 2 §2.4a.
 */
@Component({
  selector: "app-lean-engine-docs",
  standalone: true,
  imports: [RouterModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./lean-engine-docs.component.html",
  styleUrls: ["./lean-engine-docs.component.scss"],
})
export class LeanEngineDocsComponent {}
