// Общая обвязка тестов: мок /api/admin, журнал запросов и сбор ошибок консоли.
import { test as base, expect } from '@playwright/test';
import { BASE, readResponses, writeResponses } from './fixtures.js';

export const KEY = 'test-admin-key';
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');

class MockApi {
  constructor(page) {
    this.page = page;
    this.requests = [];
    this.problems = [];
    this.overrides = [];
    this.data = structuredClone(readResponses);
  }

  /** Подменить ответ: method — 'GET' и т. п., path — строка (путь с query) или RegExp. */
  on(method, path, reply) { this.overrides.unshift({ method, path, reply }); }

  /** Дождаться, пока панель перестанет обращаться к API (400 мс без новых запросов). */
  async settle() {
    let seen = -1;
    while (seen !== this.requests.length) { seen = this.requests.length; await this.page.waitForTimeout(400); }
  }

  /** Запросы, меняющие данные, в порядке отправки. */
  writes() { return this.requests.filter((r) => r.method !== 'GET'); }

  async install() {
    // Даты в моках отсчитаны от BASE: часы браузера стоят на нём, таймеры идут как обычно.
    await this.page.clock.setFixedTime(BASE);
    this.page.on('console', (m) => { if (m.type() === 'error' && !m.text().includes('status of 401')) this.problems.push(`console: ${m.text()}`); });
    this.page.on('pageerror', (e) => this.problems.push(`pageerror: ${e.message}`));
    await this.page.route('**/api/admin/**', (route) => this.handle(route));
  }

  async handle(route) {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.slice('/api/admin'.length) + url.search;
    const method = request.method();
    let body = request.postData();
    try { body = body ? JSON.parse(body) : undefined; } catch { /* файл или текст */ }
    this.requests.push({ method, path, body, key: request.headers()['x-admin-key'] });
    if (request.headers()['x-admin-key'] !== KEY) return route.fulfill({ status: 401, json: { detail: 'unauthorized' } });

    const match = (rule) => rule.method === method && (rule.path instanceof RegExp ? rule.path.test(path) : rule.path === path);
    const override = this.overrides.find(match);
    if (override) {
      const reply = typeof override.reply === 'function' ? await override.reply({ path, body }) : override.reply;
      return route.fulfill(reply?.status ? reply : { json: reply });
    }
    if (method === 'GET') {
      if (/^\/illustrations\/\d+\/thumb$/.test(path)) return route.fulfill({ body: PNG, contentType: 'image/png' });
      const key = path in this.data ? path : url.pathname.slice('/api/admin'.length);
      if (key in this.data) return route.fulfill({ json: this.data[key] });
    } else {
      const rule = writeResponses.find((r) => r.method === method && r.path.test(path));
      if (rule) return route.fulfill(rule.file ? { body: rule.file, headers: rule.headers } : { json: rule.reply });
    }
    this.problems.push(`нет мока: ${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'not_found' } });
  }
}

export const test = base.extend({
  api: async ({ page }, use) => {
    const api = new MockApi(page);
    await api.install();
    await use(api);
    // Действия перезагружают данные уже после ответа: дождаться их, чтобы запросы не ушли мимо мока.
    await api.settle();
    expect(api.problems, 'ошибки в консоли и запросы без мока').toEqual([]);
  },
});
export { expect };

/** Войти через форму входа. */
export async function login(page, key = KEY) {
  await page.goto('./');
  await page.locator('input[type=password]').fill(key);
  await page.getByRole('button', { name: 'Войти' }).click();
}

/** Открыть раздел через меню; на узком экране меню сначала выдвигается кнопкой. */
export async function openSection(page, label) {
  // Дождаться каркаса: сразу после входа меню может быть ещё не отрисовано. Открыто ли меню, смотрим по
  // aria-expanded, а не по видимости: во время анимации закрытия меню ещё видно, хотя уже закрывается.
  await page.locator('#sidebar').waitFor({ state: 'attached' });
  const toggle = page.getByRole('button', { name: 'Открыть меню' });
  if (await toggle.isVisible() && (await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
  await page.getByRole('navigation', { name: 'Разделы' }).getByRole('link', { name: label, exact: true }).click();
}

/** Принять (или отклонить) следующее окно confirm/prompt; для prompt можно передать ответ. */
export function answerDialog(page, { accept = true, text } = {}) {
  page.once('dialog', (dialog) => (accept ? dialog.accept(text) : dialog.dismiss()));
}
