import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FileDownloadService } from './file-download.service';

describe('FileDownloadService', () => {
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  let clicked: { href: string; download: string }[];

  beforeEach(() => {
    clicked = [];
    URL.createObjectURL = vi.fn(() => 'blob:the-bytes');
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ href: this.getAttribute('href') ?? '', download: this.download });
    });
  });

  afterEach(() => {
    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
    vi.restoreAllMocks();
  });

  it('downloads the bytes under the name asked and lets the link go once it has been used', () => {
    const blob = new Blob(['{"a":1}'], { type: 'application/json' });

    TestBed.inject(FileDownloadService).save(blob, 'my setup.json');

    expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
    expect(clicked).toEqual([{ href: 'blob:the-bytes', download: 'my setup.json' }]);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:the-bytes');
  });
});
