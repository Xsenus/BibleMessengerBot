// Скриншоты всех разделов на моках для сравнения до/после: `npm run screenshots`.
// Папка — SCREENSHOTS_DIR (по умолчанию admin-ui/screenshots). Не входит в `npm test`.
import path from 'node:path';
import { test, login, openSection } from './support.js';
import { SECTIONS } from './sections.js';

const OUT = process.env.SCREENSHOTS_DIR || path.resolve('screenshots');
const MODES = [
  { name: 'light', viewport: { width: 1440, height: 900 }, colorScheme: 'light' },
  { name: 'dark', viewport: { width: 1440, height: 900 }, colorScheme: 'dark' },
  { name: 'mobile', viewport: { width: 390, height: 844 }, colorScheme: 'light' },
];

for (const mode of MODES) {
  test(`скриншоты: ${mode.name}`, async ({ page, api }) => {
    test.setTimeout(120000);
    await page.setViewportSize(mode.viewport);
    await page.emulateMedia({ colorScheme: mode.colorScheme });
    await page.goto('./');
    await page.screenshot({ path: `${OUT}/${mode.name}-00-login.png` });
    await login(page);
    for (const [index, section] of SECTIONS.entries()) {
      await openSection(page, section.nav);
      await api.settle();
      const name = `${mode.name}-${String(index + 1).padStart(2, '0')}-${section.slug}.png`;
      await page.screenshot({ path: `${OUT}/${name}`, fullPage: true, animations: 'disabled' });
    }
  });
}
