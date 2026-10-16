import { defineConfig, devices } from '@playwright/test';

// Smoke-тесты панели на моках API: сервер и база не нужны.
// Браузер: `npx playwright install chromium` или готовый Chromium через PW_CHROMIUM_PATH.
const executablePath = process.env.PW_CHROMIUM_PATH || undefined;
const use = { ...devices['Desktop Chrome'], locale: 'ru-RU', timezoneId: 'Europe/Moscow', launchOptions: { executablePath } };

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: { baseURL: 'http://127.0.0.1:4173/panel/', trace: 'retain-on-failure' },
  projects: [
    { name: 'smoke', testIgnore: /screenshots\.spec\.js/, use },
    { name: 'screenshots', testMatch: /screenshots\.spec\.js/, use },
  ],
  webServer: {
    command: 'npm run build && npx vite preview --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173/panel/',
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
