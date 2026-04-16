FROM node:22-slim

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy package files and install dependencies (cached unless package*.json changes)
COPY package.json package-lock.json ./
RUN npm ci

# Copy the rest of the application
COPY . .

EXPOSE 4200

# Dev server with polling for container volume changes
CMD ["npx", "ng", "serve", "--host", "0.0.0.0", "--poll", "2000", "--proxy-config", "proxy.conf.json"]
