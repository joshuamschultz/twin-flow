import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// twinflow web — dev server proxies /api to the local FastAPI backend
// (uvicorn on :8000) so the client can fetch relative /api/... paths.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/docs": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/openapi.json": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
