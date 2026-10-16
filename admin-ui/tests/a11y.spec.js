// Доступность (WCAG 2.1 AA): axe-core проверяет вход, все разделы и окна в светлой и тёмной теме и на телефоне.
// Найденное нарушение печатается как «раздел · правило · элемент · что не так».
import AxeBuilder from '@axe-core/playwright';
import { test, expect, login, openSection } from './support.js';
import { SECTIONS } from './sections.js';

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice'];

async function scan(page, where) {
  const { violations } = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  return violations.flatMap((v) => v.nodes.map((n) => `${where} · ${v.id} · ${n.target.join(' ')} · ${n.failureSummary?.split('\n')[1]?.trim() ?? v.help}`));
}

const MODES = [
  { name: 'светлая тема', viewport: { width: 1440, height: 900 }, colorScheme: 'light' },
  { name: 'тёмная тема', viewport: { width: 1440, height: 900 }, colorScheme: 'dark' },
  { name: 'телефон', viewport: { width: 390, height: 844 }, colorScheme: 'light' },
];

for (const mode of MODES) {
  test(`доступность: вход и все разделы, ${mode.name}`, async ({ page, api }) => {
    test.setTimeout(120000);
    await page.setViewportSize(mode.viewport);
    await page.emulateMedia({ colorScheme: mode.colorScheme });
    await page.goto('./');
    const found = await scan(page, 'вход');
    await login(page);
    for (const section of SECTIONS) {
      await openSection(page, section.nav);
      await api.settle();
      found.push(...await scan(page, section.slug));
    }
    expect(found, 'нарушения доступности').toEqual([]);
  });
}

test('доступность: окно подтверждения и формы', async ({ page, api }) => {
  await login(page);
  await openSection(page, 'Доставки');
  await page.getByRole('button', { name: 'Дошло' }).first().click();
  await expect(page.locator('dialog[open]')).toBeVisible();
  const found = await scan(page, 'окно подтверждения');
  await page.locator('dialog[open] [data-dialog=cancel]').click();
  await openSection(page, 'Генераторы картинок');
  await page.getByRole('combobox').filter({ hasText: 'выберите шаблон' }).selectOption('cloudflare-flux');
  await api.settle();
  found.push(...await scan(page, 'форма генератора'));
  expect(found, 'нарушения доступности').toEqual([]);
});
