import type { SetupRequest } from './options-backtest.types';

export type SetupFile = { readonly ok: true; readonly json: unknown } | { readonly ok: false; readonly message: string };

/** Reads an import file as JSON. Whether it is a setup is the server's to say. */
export async function readSetupFile(file: File): Promise<SetupFile> {
  let text: string;
  try {
    text = await file.text();
  } catch (error) {
    return { ok: false, message: `The file could not be read: ${reasonOf(error)}` };
  }
  try {
    const json: unknown = JSON.parse(text);
    return { ok: true, json };
  } catch (error) {
    return { ok: false, message: `That file is not JSON: ${reasonOf(error)}` };
  }
}

/**
 * A body the server has just accepted as a setup, typed as one. The only way
 * an imported file becomes a draft: the schema is Python's, and the page keeps
 * no second validator of it.
 */
export function acceptedSetup(json: unknown): SetupRequest {
  // The describe route validated this exact value against the setup schema a moment ago.
  return json as SetupRequest;
}

/** The export file's bytes: exactly the body the routes accept. */
export function setupFileBlob(body: SetupRequest): Blob {
  return new Blob([`${JSON.stringify(body, null, 2)}\n`], { type: 'application/json' });
}

/** The export file's name: the setup's name, or its underlying when it has none yet. */
export function setupFileName(name: string, underlying: string): string {
  const base = name.trim() === '' ? `${underlying} setup` : name.trim();
  return `${base.replace(/[\\/:*?"<>|]/g, '-')}.json`;
}

function reasonOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
