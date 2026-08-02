# Frontend image, built in two stages.
#
# Stage 1 uses Node to compile React into plain files. Stage 2 throws Node
# away entirely and keeps only those files plus Caddy to serve them. The
# finished image is ~50 MB instead of ~1 GB, and no JavaScript toolchain is
# ever shipped to the server.
#
# Caddy also reverse-proxies /api to the backend, so the browser only ever
# talks to one origin and there is no CORS to configure in production.

# --- stage 1: build ---------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Same caching trick as the backend: dependencies install only when the
# lockfile changes, not on every source edit.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# `npm run build` is `tsc -b && vite build`, so a type error fails the image
# build rather than reaching production.
RUN npm run build

# --- stage 2: serve ---------------------------------------------------------
FROM caddy:2-alpine

COPY --from=build /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile

EXPOSE 80
