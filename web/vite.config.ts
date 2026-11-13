import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 产物挂载在后端的 /ui 下，使用相对 base + hash 路由，无需改后端路由。
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8002", changeOrigin: true },
      "/webhooks": { target: "http://127.0.0.1:8002", changeOrigin: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
