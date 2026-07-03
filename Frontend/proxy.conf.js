const DATA_PLANE_CONTROL_SECRET_HEADER = 'X-Data-Plane-Control-Secret';
const dataPlaneControlSecret = process.env.DATA_PLANE_CONTROL_SECRET ?? 'local-dev-control-secret';
const dataPlaneHeaders = dataPlaneControlSecret
  ? { [DATA_PLANE_CONTROL_SECRET_HEADER]: dataPlaneControlSecret }
  : {};

module.exports = {
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
    headers: dataPlaneHeaders,
  },
};
