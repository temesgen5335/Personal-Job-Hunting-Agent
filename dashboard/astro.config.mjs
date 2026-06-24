// @ts-check
import { defineConfig } from "astro/config";
import node from "@astrojs/node";

// SSR (server) so pages fetch live data from the FastAPI orchestrator each request.
export default defineConfig({
  output: "server",
  adapter: node({ mode: "standalone" }),
});
