const DATA_PLANE_CONTROL_SECRET_HEADER = 'X-Data-Plane-Control-Secret';
const DATA_PLANE_CONTROL_INTENT_HEADER = 'X-Data-Plane-Control-Intent';
const DATA_PLANE_CONTROL_INTENT_VALUE = 'learn-ai-browser-control';
const dataPlaneControlSecret = process.env.DATA_PLANE_CONTROL_SECRET ?? 'local-dev-control-secret';
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const CONTROL_PREFIXES = [
  '/api/broker',
  '/api/accounts',
  '/api/live-instances',
  '/api/live-runs',
];
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
  if (!value) return true;
  try {
    const parsed = new URL(value);
    return LOCAL_DEV_ORIGINS.has(parsed.origin);
  } catch {
    return false;
  }
}

function hasSameOriginFetchMetadata(req) {
  const secFetchSite = requestHeader(req, 'sec-fetch-site');
  if (secFetchSite && secFetchSite !== 'same-origin' && secFetchSite !== 'none') return false;
  return isLocalDevUrl(requestHeader(req, 'origin')) && isLocalDevUrl(requestHeader(req, 'referer'));
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
    attachDataPlaneSecret,
    isControlMutation,
    shouldAttachDataPlaneSecret,
  },
});

module.exports = proxyConfig;
