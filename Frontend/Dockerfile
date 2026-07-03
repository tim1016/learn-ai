FROM node:22-slim

WORKDIR /app

# curl: healthcheck. python3 + build-essential: node-gyp toolchain for
# native deps without prebuilt binaries for linux/arm64 (e.g. bufferutil
# transitively pulled in by ws). Removed once the native build runs in npm ci.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl python3 build-essential \
 && rm -rf /var/lib/apt/lists/*

# Copy package files and install dependencies (cached unless package*.json changes)
COPY package.json package-lock.json ./
RUN npm ci

# Copy the rest of the application
COPY . .

EXPOSE 4200

# Dev server with polling for container volume changes
CMD ["npx", "ng", "serve", "--host", "0.0.0.0", "--poll", "2000", "--proxy-config", "proxy.conf.js"]
