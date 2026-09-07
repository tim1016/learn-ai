const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// The run dock is position:fixed at the viewport bottom and publishes its
// current height as --run-dock-height on :root (retracted with the dock).
// Every page that hosts <app-run-dock> must reserve that height in its own
// stylesheet, or the last controls hide under the expanded dock (#1974).
// Hosts are found, not listed, so the next page that mounts the dock and
// forgets is caught rather than skipped.

const appRoot = path.resolve(__dirname, '../src/app');
const hosts = fs
  .readdirSync(appRoot, { recursive: true })
  .filter((entry) => entry.endsWith('.component.html'))
  .map((entry) => path.join(appRoot, entry))
  .filter((file) => fs.readFileSync(file, 'utf8').includes('<app-run-dock'));

assert.ok(hosts.length > 0, 'no template mounts <app-run-dock>; the scan is broken');

for (const template of hosts) {
  const stylesheet = template.replace(/\.component\.html$/, '.component.scss');
  const relative = path.relative(appRoot, template);
  assert.ok(fs.existsSync(stylesheet), `${relative} hosts the run dock but has no component stylesheet to reserve its height in.`);
  assert.match(
    fs.readFileSync(stylesheet, 'utf8'),
    /var\(--run-dock-height/,
    `${relative} hosts the run dock; its stylesheet must reserve var(--run-dock-height) instead of a fixed strip height.`,
  );
}

process.stdout.write(`run dock space guard ok (${hosts.length} hosts)\n`);
