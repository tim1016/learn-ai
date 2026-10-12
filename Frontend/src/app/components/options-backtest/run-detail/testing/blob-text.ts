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
