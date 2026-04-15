import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Local npm: default 127.0.0.1. Docker: set VITE_API_PROXY_TARGET=http://host.docker.internal:8095
const apiProxy = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8095";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": apiProxy,
      "/health": apiProxy,
    },
  },
});
