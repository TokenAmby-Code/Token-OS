import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Static mockup — no proxy, no backend. Just serves the React design study.
export default defineConfig({
  plugins: [react()],
  server: { port: 5199, host: true },
});
