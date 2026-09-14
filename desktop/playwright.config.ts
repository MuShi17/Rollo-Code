import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: 'tests/e2e',
  // A real window is a real resource: keep it serial and give it room to start.
  workers: 1,
  fullyParallel: false,
  timeout: 90_000,
  expect: { timeout: 30_000 },
  reporter: [['list']],
});
