/* Mini-chart fingerprint for the hover-preview popover. The Angular
 * component will swap in real data later; this is a deterministic synthetic
 * series so each indicator family gets a recognisable shape. */

export type PreviewKind =
  | 'overlay-band'
  | 'overlay-line'
  | 'sub-osc-bounded'
  | 'sub-hist'
  | 'sub-area';

export function previewKindFor(indicatorName: string, pane: 'overlay' | 'sub'): PreviewKind {
  if (pane === 'overlay') {
    return ['bbands', 'keltner', 'donchian'].includes(indicatorName)
      ? 'overlay-band'
      : 'overlay-line';
  }
  if (['rsi', 'stoch', 'willr', 'mfi', 'cci'].includes(indicatorName)) {
    return 'sub-osc-bounded';
  }
  if (['macd', 'roc', 'vroc'].includes(indicatorName)) {
    return 'sub-hist';
  }
  return 'sub-area';
}

const SAMPLE_N = 96;
let cachedSeries: number[] | null = null;
function sampleSeries(): number[] {
  if (cachedSeries) return cachedSeries;
  const series: number[] = [];
  for (let i = 0; i < SAMPLE_N; i++) {
    const t = i / SAMPLE_N;
    series.push(
      Math.sin(t * Math.PI * 3.1)
        + 0.4 * Math.sin(t * Math.PI * 7.7)
        + (((i * 9301 + 49297) % 233280) / 233280 - 0.5) * 0.45,
    );
  }
  cachedSeries = series;
  return series;
}

function ema(arr: readonly number[], len: number): number[] {
  const k = 2 / (len + 1);
  const out: number[] = [arr[0]];
  for (let i = 1; i < arr.length; i++) out.push(arr[i] * k + out[i - 1] * (1 - k));
  return out;
}

function drawSeries(
  ctx: CanvasRenderingContext2D,
  arr: readonly number[],
  xMap: (i: number) => number,
  yMap: (v: number) => number,
  color: string,
  lineWidth: number,
): void {
  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth;
  ctx.beginPath();
  for (let i = 0; i < arr.length; i++) {
    const x = xMap(i);
    const y = yMap(arr[i]);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

export function drawPreview(canvas: HTMLCanvasElement, kind: PreviewKind): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const series = sampleSeries();
  const min = Math.min(...series);
  const max = Math.max(...series);
  const range = max - min || 1;
  const yMap = (v: number): number => 4 + (h - 8) * (1 - (v - min) / range);
  const xMap = (i: number): number => (i / (SAMPLE_N - 1)) * w;

  // Baseline grid
  ctx.strokeStyle = 'rgba(255,255,255,.05)';
  ctx.lineWidth = 1;
  for (let g = 1; g < 3; g++) {
    const y = (h / 3) * g;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }

  if (kind === 'overlay-band') {
    ctx.fillStyle = 'rgba(242,173,61,.10)';
    ctx.beginPath();
    for (let i = 0; i < SAMPLE_N; i++) {
      const x = xMap(i);
      const y = yMap(series[i] + 0.55);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    for (let i = SAMPLE_N - 1; i >= 0; i--) {
      ctx.lineTo(xMap(i), yMap(series[i] - 0.55));
    }
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = 'rgba(242,173,61,.55)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i < SAMPLE_N; i++) {
      const x = xMap(i);
      const y = yMap(series[i] + 0.55);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.beginPath();
    for (let i = 0; i < SAMPLE_N; i++) {
      const x = xMap(i);
      const y = yMap(series[i] - 0.55);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    drawSeries(ctx, series, xMap, yMap, '#b2b5be', 1.2);
  } else if (kind === 'overlay-line') {
    drawSeries(ctx, series, xMap, yMap, 'rgba(178,181,190,.55)', 1);
    drawSeries(ctx, ema(series, 12), xMap, yMap, '#4d8dff', 1.6);
  } else if (kind === 'sub-osc-bounded') {
    const osc = series.map(v => 50 + v * 22);
    ctx.strokeStyle = 'rgba(167,139,250,.25)';
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(0, yMap(50 + 22 * 0.91));
    ctx.lineTo(w, yMap(50 + 22 * 0.91));
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(0, yMap(50 - 22 * 0.91));
    ctx.lineTo(w, yMap(50 - 22 * 0.91));
    ctx.stroke();
    ctx.setLineDash([]);
    drawSeries(ctx, osc, xMap, yMap, '#a78bfa', 1.6);
  } else if (kind === 'sub-hist') {
    const sig = ema(series, 5);
    const hist = series.map((v, i) => v - sig[i]);
    const hmax = Math.max(...hist.map(Math.abs)) || 1;
    const midY = h / 2;
    for (let i = 0; i < SAMPLE_N; i++) {
      const val = hist[i];
      const barH = (val / hmax) * (h / 2 - 6);
      ctx.fillStyle = val >= 0 ? 'rgba(38,166,154,.85)' : 'rgba(239,83,80,.85)';
      ctx.fillRect(xMap(i) - 1, midY, 2, -barH);
    }
    drawSeries(ctx, sig.map(v => v * 0.6), xMap, yMap, '#a78bfa', 1.4);
  } else {
    // sub-area
    const abs = series.map(v => Math.abs(v));
    ctx.fillStyle = 'rgba(242,173,61,.18)';
    ctx.beginPath();
    ctx.moveTo(0, h);
    for (let i = 0; i < SAMPLE_N; i++) ctx.lineTo(xMap(i), yMap(abs[i]));
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fill();
    drawSeries(ctx, abs, xMap, yMap, '#f2ad3d', 1.4);
  }
}
