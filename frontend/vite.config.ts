import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [react()],
  base: "/",
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8090",
      "/healthz": "http://127.0.0.1:8090",
    },
  },
  build: {
    outDir: fileURLToPath(new URL("../src/rtmp_monitor/static", import.meta.url)),
    emptyOutDir: true,
    assetsDir: "assets",
  },
});
