Verify all containers are healthy and services are responding.

1. **Container status**:
   ```bash
   podman compose ps
   ```

2. **Service health** (check each endpoint):
   - Frontend: `curl -sf http://localhost:4200 > /dev/null && echo "OK" || echo "FAIL"`
   - Backend: `curl -sf http://localhost:5000/graphql?query={__typename} > /dev/null && echo "OK" || echo "FAIL"`
   - Python: `curl -sf http://localhost:8000/docs > /dev/null && echo "OK" || echo "FAIL"`
   - Postgres: `podman exec my-postgres pg_isready -U postgres`

3. **Container logs** (last 5 lines each, check for errors):
   ```bash
   podman logs --tail 5 my-frontend
   podman logs --tail 5 my-backend
   podman logs --tail 5 polygon-data-service
   podman logs --tail 5 my-postgres
   ```

Report a summary table: service, container status, endpoint status, any errors in logs.
