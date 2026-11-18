# syntax=docker/dockerfile:1.7
# LocalOps frontend: static bundle served by unprivileged nginx.
# All assets are bundled at build time; no CDN or runtime downloads.

ARG NODE_IMAGE=node:20.18.1-alpine
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.27.3-alpine

FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime
LABEL org.opencontainers.image.title="localops-frontend" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
