const DATA_PLANE_CONTROL_SECRET_HEADER = 'X-Data-Plane-Control-Secret';
const DATA_PLANE_CONTROL_INTENT_HEADER = 'X-Data-Plane-Control-Intent';
const DATA_PLANE_CONTROL_INTENT_VALUE = 'learn-ai-browser-control';
const dataPlaneControlSurfaces = require('../contracts/data-plane-control-surfaces.json');
const dataPlaneControlSecret = process.env.DATA_PLANE_CONTROL_SECRET ?? 'local-dev-control-secret';
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const CONTROL_PREFIXES = dataPlaneControlSurfaces.control_prefixes;
const LOCAL_DEV_ORIGINS = new Set([
  'http://localhost:4200',
  'http://127.0.0.1:4200',
]);

function requestHeader(req, name) {
  const value = req.headers[name.toLowerCase()];
  return Array.isArray(value) ? value[0] : value;
}

function requestPath(req) {
  const url = req.url ?? '';
  const path = url.startsWith('/api') ? url : `/api${url.startsWith('/') ? url : `/${url}`}`;
  return path.split('?')[0];
}

function isControlMutation(req) {
  if (!UNSAFE_METHODS.has((req.method ?? '').toUpperCase())) return false;
  const path = requestPath(req);
  return CONTROL_PREFIXES.some((prefix) => path === prefix || path.startsWith(`${prefix}/`));
}

function isLocalDevUrl(value) {
  if (!value) return false;
  try {
    const parsed = new URL(value);
    return LOCAL_DEV_ORIGINS.has(parsed.origin);
  } catch {
    return false;
  }
}

function hasSameOriginFetchMetadata(req) {
  // Metadata-absent local tools do not receive the private proxy secret. The
  // Angular app sends the public intent header; browser provenance must still
  // prove this is same-origin localhost UI traffic before we attach the secret.
  const secFetchSite = requestHeader(req, 'sec-fetch-site');
  if (secFetchSite !== 'same-origin' && secFetchSite !== 'none') return false;
  const origin = requestHeader(req, 'origin');
  const referer = requestHeader(req, 'referer');
  if (origin && !isLocalDevUrl(origin)) return false;
  if (referer && !isLocalDevUrl(referer)) return false;
  return Boolean(origin || referer);
}

function shouldAttachDataPlaneSecret(req) {
  if (!dataPlaneControlSecret || !isControlMutation(req)) return false;
  return (
    requestHeader(req, DATA_PLANE_CONTROL_INTENT_HEADER) === DATA_PLANE_CONTROL_INTENT_VALUE
    && hasSameOriginFetchMetadata(req)
  );
}

function attachDataPlaneSecret(proxyReq, req) {
  if (shouldAttachDataPlaneSecret(req)) {
    proxyReq.setHeader(DATA_PLANE_CONTROL_SECRET_HEADER, dataPlaneControlSecret);
    return;
  }
  if (typeof proxyReq.removeHeader === 'function') {
    proxyReq.removeHeader(DATA_PLANE_CONTROL_SECRET_HEADER);
  }
}

function configureDataPlaneProxy(proxy) {
  proxy.on('proxyReq', attachDataPlaneSecret);
}

const proxyConfig = {
  '/graphql': {
    target: 'http://backend:8080',
    secure: false,
    changeOrigin: true,
  },
  '/api/jobs': {
    target: 'http://backend:8080',
    secure: false,
    changeOrigin: true,
  },
  '/api': {
    target: 'http://python-service:8000',
    secure: false,
    changeOrigin: true,
    configure: configureDataPlaneProxy,
    onProxyReq: attachDataPlaneSecret,
    on: {
      proxyReq: attachDataPlaneSecret,
    },
  },
};

Object.defineProperty(proxyConfig, '__test', {
  enumerable: false,
  value: {
    DATA_PLANE_CONTROL_SECRET_HEADER,
    DATA_PLANE_CONTROL_INTENT_HEADER,
    DATA_PLANE_CONTROL_INTENT_VALUE,
    CONTROL_PREFIXES,
    attachDataPlaneSecret,
    configureDataPlaneProxy,
    isControlMutation,
    shouldAttachDataPlaneSecret,
  },
});

module.exports = proxyConfig;
