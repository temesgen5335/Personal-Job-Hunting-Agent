# Job Agent Dashboard (Astro)

Web view that fetches the **FastAPI orchestrator** (v2). SSR — live data each request.
Start the API first (`scripts/run_api.py`), then point the dashboard at it via
`JOBAGENT_API_URL`.

Backend and dashboard both default to port **8077**, so no env var is needed locally.

## Run
```bash
# 1) backend (from repo root) — defaults to :8077
.venv/bin/python scripts/run_api.py

# 2) dashboard (defaults to API at http://127.0.0.1:8077)
cd dashboard
npm install
npm run dev            # http://localhost:4321
```
Production:
```bash
npm run build && npm start          # node ./dist/server/entry.mjs
# point elsewhere if needed: JOBAGENT_API_URL=http://host:port npm start
```

## Pages
- **/** — overview: counts, jobs-by-source, application funnel, top matches
- **/jobs** — filterable table (date / location / keywords) via query params
- **/applications** — application tracking (status, dates, links)

## Config
- `JOBAGENT_API_URL` — backend base URL (default `http://127.0.0.1:8077`).
- `HOST` / `PORT` — standard `@astrojs/node` standalone server vars.

Currently read-only (analytics). v2.1 adds config + actions via the API. All
mutations go through the FastAPI orchestrator, keeping the store single-writer.
