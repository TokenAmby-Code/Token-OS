import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The live ops cockpit. Served at /ui/ops from the committed build in
// token-api/ui/ops; the dev server proxies /api to the local Token-API.
export default defineConfig({
  base: '/ui/ops/',
  plugins: [react()],
  server: {
    port: 5199,
    host: true,
    proxy: { '/api': 'http://localhost:7777' },
  },
  build: {
    outDir: '../../ui/ops',
    emptyOutDir: true,
  },
});
