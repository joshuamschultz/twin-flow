import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// twinflow web — dev server proxies /api to the local FastAPI backend.
// The backend defaults to uvicorn on :8000. If that port is taken by another
// program, start the API on a different port and point the proxy at it:
//   TWINFLOW_API_TARGET=http://127.0.0.1:8010 npm run dev
const apiTarget = process.env.TWINFLOW_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/docs": { target: apiTarget, changeOrigin: true },
      "/openapi.json": { target: apiTarget, changeOrigin: true },
      "/api": {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
});
