# Job Agent Dashboard (Astro)

Read-only web view over the same SQLite store the agent writes
(`../data/jobagent.db`). SSR — live data on each request, no separate API.

## Run
```bash
cd dashboard
npm install            # first time (builds better-sqlite3)
npm run dev            # http://localhost:4321
```
Production:
```bash
npm run build
JOBAGENT_DB_PATH=/abs/path/to/data/jobagent.db npm start   # node ./dist/server/entry.mjs
```

## Pages
- **/** — overview: counts, jobs-by-source, application funnel, top matches
- **/jobs** — filterable table (date / location / keywords) via query params
- **/applications** — application tracking (status, dates, links)

## Config
- `JOBAGENT_DB_PATH` — absolute path to the store (defaults to `../data/jobagent.db`).
- `HOST` / `PORT` — standard `@astrojs/node` standalone server vars.

Read-only by design: the dashboard never writes. All mutations go through the
FastAPI/bot path (the agent), keeping the store single-writer.
