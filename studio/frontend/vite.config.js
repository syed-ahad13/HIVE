// ===========================================================================
// HIVE STUDIO FRONTEND — VITE CONFIG
// ===========================================================================
// Two settings, both fixed by CLAUDE.md §6.
//
// PORT 5173 is the frontend. PORT 8000 is the BFF, the only thing that talks to
// the cluster.
//
// THE PROXY IS WHY THERE IS NO CORS LAYER ANYWHERE IN THIS PROJECT. Anything the
// browser requests under /api is forwarded to the BFF by this dev server, so as
// far as the browser is concerned every request is same-origin. Without it the
// backend would need permissive cross-origin headers — a real hole opened to
// solve a problem a three-line proxy removes.
//
// It also means the frontend never learns the backend's address. `api.js` fetches
// relative paths like `/api/templates` and nothing in src/ contains a hostname or
// a port, so the same code works unchanged when the two are served together.
// ===========================================================================

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Fail loudly rather than silently moving to 5174 if something already holds
    // the port — a second copy of the console on an unexpected port is a
    // confusing thing to debug during a demo.
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
