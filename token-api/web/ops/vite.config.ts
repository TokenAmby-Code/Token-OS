import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: '/ui/ops/',
  plugins: [react()],
  build: {
    outDir: '../../ui/ops',
    emptyOutDir: true,
  },
});
