/** jsdom's Blob has no `text()`; a browser's does. A spec that reads a Blob's text calls this once before it does. */
export function ensureBlobText(): void {
  if (typeof Blob.prototype.text === 'function') return;
  Blob.prototype.text = function (this: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(this);
    });
  };
}

/** The text of a Blob, for a spec that checks the bytes a download was handed. */
export function textOf(blob: Blob): Promise<string> {
  ensureBlobText();
  return blob.text();
}

/** The bytes of a Blob exactly as they are, for a spec that must tell a file that was decoded and written again from one that was not. */
export function bytesOf(blob: Blob): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}
