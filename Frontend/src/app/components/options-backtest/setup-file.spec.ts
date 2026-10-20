import { describe, expect, it, vi } from 'vitest';

import { exampleBody } from './testing/fixtures';
import { MAX_SETUP_FILE_BYTES, readSetupFile, setupFileBlob, setupFileName } from './setup-file';

function textOf(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

function fileOf(text: string): File {
  const file = new File([text], 'x.json');
  // jsdom's File has no text(); a browser's does.
  if (typeof file.text !== 'function') Object.defineProperty(file, 'text', { value: () => textOf(file) });
  return file;
}

describe('the export file', () => {
  it('holds exactly the body, in JSON, readable and ending in a new line, and is named for the setup, or for its underlying when it has no name, with nothing a file name cannot hold', async () => {
    const blob = setupFileBlob(exampleBody());

    expect.soft(blob.type, 'holds exactly the body, in JSON: the file type').toBe('application/json');
    const text = await textOf(blob);
    expect.soft(text, 'holds exactly the body, readable and ending in a new line').toBe(`${JSON.stringify(exampleBody(), null, 2)}\n`);
    expect.soft(JSON.parse(text), 'holds exactly the body, in JSON: it parses back to the body').toEqual(exampleBody());
    expect.soft(setupFileName('My straddle', 'SPY'), 'is named for the setup').toBe('My straddle.json');
    expect.soft(setupFileName('  padded  ', 'SPY'), 'is named for the setup, trimmed').toBe('padded.json');
    expect.soft(setupFileName('', 'AAPL'), 'is named for its underlying when it has no name').toBe('AAPL setup.json');
    expect.soft(setupFileName('a/b:c', 'SPY'), 'is named with nothing a file name cannot hold').toBe('a-b-c.json');
  });
});

describe('reading an import file', () => {
  it('hands back whatever JSON it holds, for the server to judge, says that it is not JSON or could not be read, and reads a file of exactly a megabyte', async () => {
    await expect.soft(readSetupFile(fileOf('{"underlying":"SPY"}')), 'hands back whatever JSON it holds, for the server to judge: an object').resolves.toEqual({ ok: true, json: { underlying: 'SPY' } });
    await expect.soft(readSetupFile(fileOf('[1, 2]')), 'hands back whatever JSON it holds, for the server to judge: an array').resolves.toEqual({ ok: true, json: [1, 2] });

    const read = await readSetupFile(fileOf('{ not json'));

    expect.soft(read.ok, 'says that it is not JSON when it is not').toBe(false);
    expect.soft(read, 'says that it is not JSON, and why, when it is not').toMatchObject({ message: expect.stringContaining('not JSON') });

    const gone = new File([''], 'gone.json');
    Object.defineProperty(gone, 'text', { value: () => Promise.reject(new Error('permission denied')) });

    await expect.soft(readSetupFile(gone), 'says that it could not be read, when the browser cannot read it').resolves.toEqual({ ok: false, message: 'The file could not be read: permission denied' });

    const edge = new File([new Uint8Array(MAX_SETUP_FILE_BYTES)], 'edge.json');
    Object.defineProperty(edge, 'text', { value: async () => '{}' });

    await expect.soft(readSetupFile(edge), 'reads a file of exactly a megabyte').resolves.toEqual({ ok: true, json: {} });
  });

  it('refuses a file over a megabyte without reading it, and says why', async () => {
    const file = new File([new Uint8Array(MAX_SETUP_FILE_BYTES + 1)], 'huge.json');
    const text = vi.fn(async () => '{}');
    Object.defineProperty(file, 'text', { value: text });

    const read = await readSetupFile(file);

    expect(read).toMatchObject({ ok: false, message: expect.stringContaining('over 1 MB') });
    expect(text).not.toHaveBeenCalled();
  });
});
