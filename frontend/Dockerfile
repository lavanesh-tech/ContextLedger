# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE=node:22-bookworm-slim

# ---- dependencies from the lock file --------------------------------------------
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# ---- build (standalone server output) -----------------------------------------
FROM ${NODE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
# The API address is compiled into the rewrite rules at build time.
ARG CONTEXTLEDGER_API_URL=http://api:8000
ENV CONTEXTLEDGER_API_URL=${CONTEXTLEDGER_API_URL}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- runtime: only the standalone server, non-root ----------------------------
FROM ${NODE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
LABEL org.opencontainers.image.title="contextledger-web" \
      org.opencontainers.image.source="https://github.com/lavanesh-tech/ContextLedger"
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
