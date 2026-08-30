import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// twinflow web — dev server proxies /api to the local FastAPI backend
// (uvicorn on :8000) so the client can fetch relative /api/... paths.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
