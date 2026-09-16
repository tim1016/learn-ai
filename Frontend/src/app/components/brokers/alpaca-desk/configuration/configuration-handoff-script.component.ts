import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { CopyButtonComponent } from '../../../../shared/copy-button/copy-button.component';

/**
 * The pieces of a `worker_restart_command` invocation
 * (`desk_state.py`), in the exact token order that function guarantees:
 * `podman compose [--project-name P] [-f F]... [--profile PR] restart S`.
 */
interface WorkerRestartFields {
  readonly service: string;
  readonly composeProject: string | null;
  readonly composeFiles: readonly string[];
  readonly composeProfile: string | null;
}

/**
 * Decode the desk-authored restart command back into the pieces the handoff
 * script's own derivation needs. This never guesses a lane's worker service
 * on its own — it only reads back what `worker_restart_command` already
 * computed for this exact lane, in the token order its own docstring pins
 * ("Compose reads its global options before the subcommand ... so the order
 * below is the command's order and not a style choice"). Any shape that
 * doesn't match — including an embedded single quote a shell substitution
 * could not carry safely — yields `null` rather than a guessed field.
 */
function parseWorkerRestartCommand(command: string): WorkerRestartFields | null {
  const tokens = command.trim().split(/\s+/);
  if (tokens[0] !== 'podman' || tokens[1] !== 'compose' || tokens.length < 4) return null;

  let composeProject: string | null = null;
  const composeFiles: string[] = [];
  let composeProfile: string | null = null;
  let index = 2;
  while (index < tokens.length - 2) {
    const flag = tokens[index];
    const value = tokens[index + 1];
    if (value === undefined || value.includes("'")) return null;
    if (flag === '--project-name') {
      composeProject = value;
    } else if (flag === '-f') {
      composeFiles.push(value);
    } else if (flag === '--profile') {
      composeProfile = value;
    } else {
      return null;
    }
    index += 2;
  }
  const service = tokens[index + 1];
  if (tokens[index] !== 'restart' || service === undefined || service.includes("'")) return null;
  return { service, composeProject, composeFiles, composeProfile };
}

/**
 * `export` lines for the variables the script's own derivation reads. Only
 * the pieces this deployment actually declared are emitted — an unset one
 * must stay unset, not `''`, so the script's own `${VAR:-}` fallback (bare
 * `podman compose`) still applies exactly as it does when the operator
 * exports these by hand.
 */
function workerExportLines(fields: WorkerRestartFields): string {
  const lines = [`export FLEET_WORKER_SERVICE='${fields.service}'`];
  if (fields.composeProject !== null) {
    lines.push(`export FLEET_COMPOSE_PROJECT='${fields.composeProject}'`);
  }
  if (fields.composeFiles.length > 0) {
    lines.push(`export FLEET_COMPOSE_FILES='${fields.composeFiles.join(',')}'`);
  }
  if (fields.composeProfile !== null) {
    lines.push(`export FLEET_COMPOSE_PROFILE='${fields.composeProfile}'`);
  }
  return lines.join('\n');
}

/**
 * Splice this lane's exact worker-restart export lines in right after
 * `set -euo pipefail`, so the copied script never falls through to the
 * required-variable refusal it carries for anyone who opens the raw asset
 * directly (the script's own header comment explains why it has no
 * default). Returns the script untouched when the command doesn't parse or
 * the anchor line is missing — a changed asset must not receive a silently
 * wrong splice.
 */
export function withWorkerExports(script: string, restartCommand: string): string {
  const fields = parseWorkerRestartCommand(restartCommand);
  if (fields === null) return script;
  const anchor = 'set -euo pipefail\n';
  const anchorIndex = script.indexOf(anchor);
  if (anchorIndex === -1) return script;
  const insertAt = anchorIndex + anchor.length;
  return `${script.slice(0, insertAt)}\n${workerExportLines(fields)}\n${script.slice(insertAt)}`;
}

/**
 * Read-only presentation of the repo-owned Paper-to-Live operator script.
 *
 * The shell asset is the executable source of truth: this component fetches
 * those exact bytes instead of maintaining a second template, then splices
 * in this lane's own worker-restart export lines (see `withWorkerExports`)
 * before the operator ever copies or reads it. Those exports come from the
 * same desk read the switch guide's restart command does
 * (`AlpacaConfigurationPageComponent.restartCommand`, sourced from
 * `deskState.value().restart_command`) — the process where compose actually
 * injected `FLEET_WORKER_SERVICE`, never composed here.
 */
@Component({
  selector: 'app-configuration-handoff-script',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CopyButtonComponent],
  templateUrl: './configuration-handoff-script.component.html',
  styleUrl: './configuration-handoff-script.component.scss',
})
export class ConfigurationHandoffScriptComponent {
  private readonly http = inject(HttpClient);
  protected readonly scriptUrl = '/assets/scripts/alpaca-paper-to-live-handoff.sh';
  /**
   * This lane's own restart command, from the same desk read the switch
   * guide renders: `null` when the deployment declared no worker service,
   * `undefined` while the read is in flight or failed. Never composed here.
   */
  readonly restartCommand = input.required<string | null | undefined>();
  protected readonly script = resource({
    loader: () => firstValueFrom(this.http.get(this.scriptUrl, { responseType: 'text' })),
  });
  /** The exact bytes the operator sees and copies. Empty until the asset
   * has loaded, so the copy button never offers to copy `undefined`. */
  protected readonly renderedScript = computed(() => {
    if (!this.script.hasValue()) return '';
    const command = this.restartCommand();
    return command ? withWorkerExports(this.script.value(), command) : this.script.value();
  });
}
