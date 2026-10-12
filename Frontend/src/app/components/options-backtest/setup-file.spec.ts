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
  it('holds exactly the body, in JSON, readable and ending in a new line', async () => {
    const blob = setupFileBlob(exampleBody());

    expect(blob.type).toBe('application/json');
    const text = await textOf(blob);
    expect(text).toBe(`${JSON.stringify(exampleBody(), null, 2)}\n`);
    expect(JSON.parse(text)).toEqual(exampleBody());
  });

  it('is named for the setup, or for its underlying when it has no name, with nothing a file name cannot hold', () => {
    expect(setupFileName('My straddle', 'SPY')).toBe('My straddle.json');
    expect(setupFileName('  padded  ', 'SPY')).toBe('padded.json');
    expect(setupFileName('', 'AAPL')).toBe('AAPL setup.json');
    expect(setupFileName('a/b:c', 'SPY')).toBe('a-b-c.json');
  });
});

describe('reading an import file', () => {
  it('hands back whatever JSON it holds, for the server to judge', async () => {
    await expect(readSetupFile(fileOf('{"underlying":"SPY"}'))).resolves.toEqual({ ok: true, json: { underlying: 'SPY' } });
    await expect(readSetupFile(fileOf('[1, 2]'))).resolves.toEqual({ ok: true, json: [1, 2] });
  });

  it('says that it is not JSON, and why, when it is not', async () => {
    const read = await readSetupFile(fileOf('{ not json'));

    expect(read.ok).toBe(false);
    expect(read).toMatchObject({ message: expect.stringContaining('not JSON') });
  });

  it('says that it could not be read, when the browser cannot read it', async () => {
    const file = new File([''], 'gone.json');
    Object.defineProperty(file, 'text', { value: () => Promise.reject(new Error('permission denied')) });

    await expect(readSetupFile(file)).resolves.toEqual({ ok: false, message: 'The file could not be read: permission denied' });
  });

  it('refuses a file over a megabyte without reading it, and says why', async () => {
    const file = new File([new Uint8Array(MAX_SETUP_FILE_BYTES + 1)], 'huge.json');
    const text = vi.fn(async () => '{}');
    Object.defineProperty(file, 'text', { value: text });

    const read = await readSetupFile(file);

    expect(read).toMatchObject({ ok: false, message: expect.stringContaining('over 1 MB') });
    expect(text).not.toHaveBeenCalled();
  });

  it('reads a file of exactly a megabyte', async () => {
    const file = new File([new Uint8Array(MAX_SETUP_FILE_BYTES)], 'edge.json');
    Object.defineProperty(file, 'text', { value: async () => '{}' });

    await expect(readSetupFile(file)).resolves.toEqual({ ok: true, json: {} });
  });
});
