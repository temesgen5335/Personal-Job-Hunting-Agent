# Astro SSR dashboard. Built and served by Node; it fetches the FastAPI orchestrator.
FROM node:20-slim AS build
WORKDIR /app

# npm ci needs the lockfile — which is why it is committed (see docs/ROADMAP.md item 3).
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM node:20-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production HOST=0.0.0.0 PORT=1234

COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=build /app/dist ./dist

EXPOSE 1234
CMD ["node", "./dist/server/entry.mjs"]
