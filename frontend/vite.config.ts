import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The backend listens on 127.0.0.1:8000 (see backend/.env.example). During
// development the Vite server proxies /api there, so the browser sees one origin
// and no CORS is needed (none is configured, on either side).
//
// changeOrigin must stay false: the backend accepts a state-changing request only
// if its Origin header equals the request's own origin (scheme + Host). Keeping
// the browser's Host (127.0.0.1:5173) makes the proxied request same-origin;
// rewriting it to :8000 would make every POST fail with 403 "origin_rejected".
// The HttpOnly, SameSite=Strict session cookie passes through unchanged.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true, // never move to another port (that would be another origin)
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: false, ws: false },
    },
  },
  build: { outDir: "dist", sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true, // lets Testing Library clean up the DOM automatically after each test
    unstubGlobals: true, // fetch stubs never leak between tests
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
