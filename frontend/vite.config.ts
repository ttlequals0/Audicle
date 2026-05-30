import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["favicon.svg"],
      workbox: {
        // Without this, the SW's navigate-fallback serves index.html for ANY
        // navigation -- including /api/v1/docs, /rss, /media, /health -- so the
        // browser gets the SPA shell and the router bounces unknown paths to /.
        // Keep these server-owned routes off the SPA fallback so they hit the
        // network directly.
        navigateFallbackDenylist: [/^\/api\//, /^\/rss\//, /^\/media\//, /^\/health\b/],
      },
      manifest: {
        name: "Audicle",
        short_name: "Audicle",
        description: "Article-to-podcast self-hosted admin",
        theme_color: "#040405",
        background_color: "#040405",
        display: "standalone",
        start_url: "/",
        icons: [
          {
            src: "/icon-192.png",
            sizes: "192x192",
            type: "image/png",
          },
          {
            src: "/icon-512.png",
            sizes: "512x512",
            type: "image/png",
          },
        ],
      },
    }),
  ],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/rss": "http://localhost:8000",
      "/media": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
