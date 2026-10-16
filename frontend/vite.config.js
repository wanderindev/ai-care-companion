import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'

// Local dev: the SPA calls same-origin /api/v1/... and Vite proxies it to the
// backend (8010 on the host; http://backend:8000 inside docker compose). In
// production the edge nginx does the same routing, so there is no CORS.
const apiProxyTarget = process.env.API_PROXY_TARGET || 'http://localhost:8010'

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: 'autoUpdate',
      // workbox.globPatterns (svg/png) already precaches public/ icons and the
      // manifest icons; listing them here too would precache them twice.
      includeManifestIcons: false,
      manifest: {
        name: 'n16chat — Salud de Papá y Mamá',
        short_name: 'n16chat',
        description: 'Bitácora médica, citas, medicamentos y laboratorios de la familia.',
        lang: 'es',
        start_url: '/',
        scope: '/',
        display: 'standalone',
        orientation: 'portrait',
        background_color: '#f8fafc',
        theme_color: '#106c69',
        icons: [
          { src: '/icons/pwa-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: '/icons/pwa-512x512.png', sizes: '512x512', type: 'image/png' },
          {
            src: '/icons/maskable-512x512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        // Only the app shell is cached. API responses (medical data) are never
        // cached by the service worker, and /api is never answered with the SPA.
        navigateFallbackDenylist: [/^\/api\//],
        // The default (js/css/html) would leave out the bundled logo SVG.
        globPatterns: ['**/*.{js,css,html,svg,png}'],
      },
    }),
  ],
  server: {
    host: true,
    port: 5180,
    strictPort: true,
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
  preview: {
    port: 5180,
    strictPort: true,
  },
})
