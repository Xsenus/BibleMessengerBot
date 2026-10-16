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

// Скриншоты для пользовательской документации (docs/assets/screenshots/panel-*.png):
// `npm run screenshots:docs`. Светлая тема, окно 1180×820, тестовые данные из моков.
const DOCS = path.resolve('../docs/assets/screenshots');
const DOC_SHOTS = [
  ['panel-overview', 'Состояние'], ['panel-announcements', 'Объявления'], ['panel-sources', 'Источники'],
  ['panel-videos', 'Видео'], ['panel-images', 'Генераторы картинок'], ['panel-chats', 'Чаты и пользователи'],
  ['panel-deliveries', 'Доставки'], ['panel-admins', 'Администраторы'], ['panel-settings', 'Настройки'],
  ['panel-network', 'Сеть и прокси'],
];

test('скриншоты для документации', async ({ page, api }) => {
  test.skip(!process.env.DOCS_SCREENSHOTS, 'только по npm run screenshots:docs');
  test.setTimeout(120000);
  await page.setViewportSize({ width: 1180, height: 820 });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('./');
  await page.screenshot({ path: `${DOCS}/panel-login.png` });
  await login(page);
  for (const [name, label] of DOC_SHOTS) {
    await openSection(page, label);
    await api.settle();
    await page.screenshot({ path: `${DOCS}/${name}.png`, animations: 'disabled' });
  }
  await openSection(page, 'Генераторы картинок');
  await page.getByRole('combobox').filter({ hasText: 'выберите шаблон' }).selectOption('cloudflare-flux');
  await api.settle();
  await page.getByRole('heading', { name: 'Добавить генератор' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${DOCS}/panel-images-template.png`, fullPage: true, animations: 'disabled' });
});
