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

test.describe('адреса разделов', () => {
  test('раздел открывается по адресу и переживает обновление страницы', async ({ page, api }) => {
    await login(page);
    await openSection(page, 'Сеть и прокси');
    await expect(page).toHaveURL(/#\/network$/);
    await expect(page).toHaveTitle('Сеть и прокси — Библия · панель');
    await page.reload();
    await expect(heading(page, 'Сеть и прокси')).toBeVisible();
    await page.goto('./#/backups');
    await expect(heading(page, 'Резервные копии')).toBeVisible();
    void api;
  });

  test('кнопка «Назад» возвращает к предыдущему разделу', async ({ page, api }) => {
    await login(page);
    await openSection(page, 'Объявления');
    await openSection(page, 'Журнал');
    await page.goBack();
    await expect(heading(page, 'Объявления')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Объявления', exact: true })).toHaveAttribute('aria-current', 'page');
    void api;
  });

  test('неизвестный адрес открывает «Состояние»', async ({ page, api }) => {
    await page.goto('./#/no-such-section');
    await page.locator('input[type=password]').fill('test-admin-key');
    await page.getByRole('button', { name: 'Войти' }).click();
    await expect(heading(page, 'Состояние')).toBeVisible();
    void api;
  });
});

test('меню на телефоне открывается, закрывается по Esc и после выбора раздела', async ({ page, api }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  const nav = page.getByRole('navigation', { name: 'Разделы' });
  await expect(nav).toBeHidden();
  await page.getByRole('button', { name: 'Открыть меню' }).click();
  await expect(nav).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(nav).toBeHidden();
  await openSection(page, 'Видео');
  await expect(nav).toBeHidden();
  await expect(page.locator('.topbar-title')).toHaveText('Видео');
  void api;
});

test('тема переключается и запоминается', async ({ page, api }) => {
  await login(page);
  const button = page.getByRole('button', { name: /^Тема:/ });
  await expect(button).toHaveText('Тема: как в системе');
  await button.click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await button.click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  void api;
});

// Разделы, уже переведённые на новые компоненты, не должны быть шире экрана телефона.
const RESPONSIVE = ['Состояние', 'Доставки', 'Отчёты'];
test('переделанные разделы помещаются в ширину телефона', async ({ page, api }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  for (const label of RESPONSIVE) {
    await openSection(page, label);
    await api.settle();
    const width = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(width, label).toBeLessThanOrEqual(390);
  }
});
