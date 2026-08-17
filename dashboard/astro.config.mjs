// @ts-check
import { defineConfig } from "astro/config";
import node from "@astrojs/node";

// SSR (server) so pages fetch live data from the FastAPI orchestrator each request.
export default defineConfig({
  output: "server",
  adapter: node({ mode: "standalone" }),
  // Pinned here as well as in the Makefile's DASH_PORT, so a bare `npm run dev`
  // lands on the same port the docs and the CORS allow-list name.
  server: { port: 1234 },
});
