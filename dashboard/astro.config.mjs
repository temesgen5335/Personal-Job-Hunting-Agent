// @ts-check
import { defineConfig } from "astro/config";
import node from "@astrojs/node";

// SSR (server) so pages read the live SQLite store on each request.
export default defineConfig({
  output: "server",
  adapter: node({ mode: "standalone" }),
  // better-sqlite3 is a native module — keep it out of the SSR bundle.
  vite: { ssr: { external: ["better-sqlite3"] } },
});
