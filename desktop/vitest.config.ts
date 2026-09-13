import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // Main-process and shared logic only: the renderer is exercised through the
    // real Electron window in the Playwright smoke, not in a DOM emulator.
    include: ['tests/main/**/*.test.ts'],
    environment: 'node',
    testTimeout: 20000,
  },
});
