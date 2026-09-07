const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// The trading chart observes its scroll wrap and sizes the canvas from that
// measurement, so anything that lets a scrollbar change the wrap's measured
// box closes a ResizeObserver -> resize -> ResizeObserver loop that runs at
// the frame rate forever (25,120 resize events in 13 idle seconds when it was
// last open). `contentRect` excludes scrollbars, so the gutter must be
// reserved whether or not the bar is drawn, and horizontal scroll must be off.
// The vertical scroll itself is deliberate: the short-viewport fallback in
// `paneHeights` overflows rather than squash a pane to nothing.
//
// Every match is anchored to the start of a line so a commented-out
// declaration cannot satisfy it — this guard is the only cover for the CSS
// half of the fix, which no unit test can reach.

const stylesheet = path.resolve(__dirname, '../src/app/shared/trading-chart/trading-chart.component.scss');
const css = fs.readFileSync(stylesheet, 'utf8');
const wrap = css.match(/^\.trading-chart__canvas-wrap\s*\{([\s\S]*?)\n\}/m);

assert.ok(wrap, 'no .trading-chart__canvas-wrap rule found; the guard is looking at the wrong file.');
assert.doesNotMatch(
  wrap[1],
  /^\s*overflow:/m,
  'the chart scroll wrap must not use the `overflow` shorthand: `overflow: auto` lets a horizontal scrollbar change the measured height and reopens the resize loop.',
);
assert.match(
  wrap[1],
  /^\s*scrollbar-gutter:\s*stable\s*;/m,
  'the chart scroll wrap must reserve `scrollbar-gutter: stable`, or a vertical scrollbar changes the measured width and reopens the resize loop.',
);
assert.match(
  wrap[1],
  /^\s*overflow-x:\s*hidden\s*;/m,
  'the chart scroll wrap must set `overflow-x: hidden`, or a horizontal scrollbar changes the measured height and reopens the resize loop.',
);

const canvas = css.match(/^\.trading-chart__canvas\s*\{([\s\S]*?)\n\}/m);
assert.ok(canvas, 'no .trading-chart__canvas rule found; the guard is looking at the wrong file.');
assert.match(
  canvas[1],
  /^\s*display:\s*block\s*;/m,
  'the chart canvas must be `display: block`; an inline box adds a baseline gap that makes it overflow the wrap by a pixel on its own.',
);

process.stdout.write('chart resize loop guard ok\n');
