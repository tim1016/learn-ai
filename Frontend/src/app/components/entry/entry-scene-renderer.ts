import { FOCUS_DEPTH, type FrameSize, type SceneOffset, type SceneView } from './entry-scene';

/** The three pictures the scene is drawn from, all in painting coordinates. */
export interface SceneTextures {
  readonly painting: TexImageSource;
  /** Red = nearness, 1 nearest. */
  readonly depth: TexImageSource;
  /** Red = the carved lettering, green = a soft halo around it. */
  readonly glow: TexImageSource;
}

export interface SceneRenderer {
  resize(frame: FrameSize, pixelRatio: number): void;
  /** `glow` runs 0..1: how lit the carved lettering is. */
  draw(view: SceneView, offset: SceneOffset, glow: number): void;
  dispose(): void;
}

const VERTEX_SHADER = `
  attribute vec2 corner;
  varying vec2 frameUv;
  void main() {
    frameUv = vec2(corner.x * 0.5 + 0.5, 0.5 - corner.y * 0.5);
    gl_Position = vec4(corner, 0.0, 1.0);
  }`;

// For each screen point, march from the nearest depth layer back and stop at
// the first one the depth map reaches. Near things then hide far things when
// the scene shifts, instead of smearing through them.
const FRAGMENT_SHADER = `
  #ifdef GL_FRAGMENT_PRECISION_HIGH
  precision highp float;
  #else
  precision mediump float;
  #endif
  uniform sampler2D painting, depth, glow;
  uniform vec2 viewCentre, viewSpan, shift;
  uniform float focusDepth, lit;
  varying vec2 frameUv;
  const int LAYERS = 28;
  void main() {
    vec2 base = viewCentre + (frameUv - 0.5) * viewSpan;
    // outside the painting the page's own backdrop shows through
    if (base.x < 0.0 || base.x > 1.0 || base.y < 0.0 || base.y > 1.0) { gl_FragColor = vec4(0.0); return; }
    vec2 previous = base + shift * (1.0 - focusDepth);
    float previousGap = texture2D(depth, previous).r - 1.0;
    vec2 hit = base - shift * focusDepth;
    for (int i = 1; i <= LAYERS; i++) {
      float layer = 1.0 - float(i) / float(LAYERS);
      vec2 candidate = base + shift * (layer - focusDepth);
      float gap = texture2D(depth, candidate).r - layer;
      if (gap >= 0.0) { hit = mix(previous, candidate, previousGap / min(previousGap - gap, -0.00001)); break; }
      previous = candidate; previousGap = gap;
    }
    hit = abs(hit); hit = 1.0 - abs(1.0 - hit);   // mirror the last few pixels at the painting's edge
    vec3 colour = texture2D(painting, hit).rgb;
    vec2 carving = texture2D(glow, hit).rg;
    vec3 ember = mix(vec3(0.86, 0.20, 0.08), vec3(1.0, 0.74, 0.30), lit);
    colour = mix(colour, ember, carving.r * min(1.0, lit * 1.25));
    colour += vec3(1.0, 0.34, 0.10) * carving.g * lit * 0.6;
    gl_FragColor = vec4(colour, 1.0);
  }`;

/**
 * Draws the entry painting with depth on `canvas`.
 *
 * Returns `null` when the browser has no WebGL; throws when the shaders will
 * not build. Either way the caller keeps showing the plain painting.
 */
export function createSceneRenderer(
  canvas: HTMLCanvasElement,
  textures: SceneTextures,
): SceneRenderer | null {
  const gl = canvas.getContext('webgl', { antialias: false, alpha: true, premultipliedAlpha: true });
  if (gl === null) return null;

  const program = buildProgram(gl);
  gl.useProgram(program);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const corner = gl.getAttribLocation(program, 'corner');
  gl.enableVertexAttribArray(corner);
  gl.vertexAttribPointer(corner, 2, gl.FLOAT, false, 0, 0);

  const uniform = (name: string) => gl.getUniformLocation(program, name);
  const sources: readonly (readonly [string, TexImageSource])[] = [
    ['painting', textures.painting],
    ['depth', textures.depth],
    ['glow', textures.glow],
  ];
  sources.forEach(([name, source], unit) => {
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
    // The pictures are not power-of-two sized, which WebGL 1 only samples clamped and unmipped.
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    gl.uniform1i(uniform(name), unit);
  });
  gl.uniform1f(uniform('focusDepth'), FOCUS_DEPTH);
  const viewCentre = uniform('viewCentre');
  const viewSpan = uniform('viewSpan');
  const shift = uniform('shift');
  const lit = uniform('lit');

  return {
    resize(frame, pixelRatio) {
      canvas.width = Math.round(frame.width * pixelRatio);
      canvas.height = Math.round(frame.height * pixelRatio);
      gl.viewport(0, 0, canvas.width, canvas.height);
    },
    draw(view, offset, glow) {
      gl.uniform2f(viewCentre, view.cx, view.cy);
      gl.uniform2f(viewSpan, view.spanX, view.spanY);
      gl.uniform2f(shift, offset.x, offset.y);
      gl.uniform1f(lit, glow);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    },
    dispose() {
      gl.getExtension('WEBGL_lose_context')?.loseContext();
    },
  };
}

function buildProgram(gl: WebGLRenderingContext): WebGLProgram {
  const program = gl.createProgram();
  gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER));
  gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(`Entry scene shaders did not link: ${gl.getProgramInfoLog(program) ?? 'no log'}`);
  }
  return program;
}

function compile(gl: WebGLRenderingContext, kind: number, source: string): WebGLShader {
  const shader = gl.createShader(kind);
  if (shader === null) throw new Error('Entry scene could not create a shader.');
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(`Entry scene shader did not compile: ${gl.getShaderInfoLog(shader) ?? 'no log'}`);
  }
  return shader;
}
