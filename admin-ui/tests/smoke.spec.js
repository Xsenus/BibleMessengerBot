// Каждый раздел открывается, показывает данные из API и не пишет ошибок в консоль.
import { test, expect, login, openSection } from './support.js';
import { SECTIONS } from './sections.js';

// Заголовок раздела: имя может включать кнопки рядом с ним («Состояние Обновить»).
const heading = (page, name) => page.getByRole('heading', { name: new RegExp(`^${name}`) }).first();

test.describe('вход', () => {
  test('неверный ключ не пускает в панель', async ({ page, api }) => {
    await login(page, 'wrong-key');
    await expect(page.getByText('Неверный ключ')).toBeVisible();
    await expect(page.getByRole('navigation')).toHaveCount(0);
    expect(api.requests.map((r) => r.path)).toEqual(['/overview']);
  });

  test('верный ключ открывает «Состояние», «Выйти» возвращает к входу', async ({ page, api }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Состояние', level: 2 })).toBeVisible();
    expect(api.requests.every((r) => r.key === 'test-admin-key')).toBe(true);
    await page.getByRole('button', { name: 'Выйти' }).click();
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
  });
});

for (const section of SECTIONS) {
  test(`раздел «${section.nav}» открывается и показывает данные`, async ({ page, api }) => {
    await login(page);
    await openSection(page, section.nav);
    await expect(heading(page, section.heading)).toBeVisible();
    for (const text of section.texts) await expect(page.getByText(text).filter({ visible: true }).first()).toBeVisible();
    void api;
  });
}

test('ошибка API показывается в разделе, а не ломает панель', async ({ page, api }) => {
  api.on('GET', '/audit?limit=200', { status: 500, json: { detail: 'Сервис временно недоступен' } });
  await login(page);
  await openSection(page, 'Журнал');
  await expect(page.getByText('Сервис временно недоступен')).toBeVisible();
  await openSection(page, 'Настройки');
  await expect(page.getByRole('heading', { name: 'Настройки' })).toBeVisible();
  api.problems = api.problems.filter((p) => !p.includes('status of 500'));
});

test('на телефоне все разделы доступны', async ({ page, api }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  for (const section of SECTIONS) {
    await openSection(page, section.nav);
    await expect(heading(page, section.heading)).toBeVisible();
  }
  void api;
});
