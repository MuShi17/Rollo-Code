import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Loaded as CommonJS by Vite because the package does not declare `type: module`.
export default defineConfig({
  root: 'src/renderer',
  base: './',
  build: {
    outDir: '../../dist/renderer',
    emptyOutDir: true,
    // The renderer is local-only; a source map would ship the whole app twice.
    sourcemap: false,
  },
  plugins: [react()],
});
