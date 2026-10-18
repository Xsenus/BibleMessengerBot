// Ключевые действия отправляют на сервер те же запросы, что и до редизайна.
import { test, expect, login, openSection, answerDialog } from './support.js';

test.beforeEach(async ({ page, api }) => { void api; await login(page); });

test('объявление: создание по дням недели', async ({ page, api }) => {
  await openSection(page, 'Объявления');
  await page.getByPlaceholder('Заголовок (необязательно)').fill('Субботнее чтение');
  await page.getByPlaceholder('Текст').fill('Читаем Евангелие от Иоанна');
  await page.getByRole('checkbox', { name: 'пт' }).check();
  await page.getByRole('checkbox', { name: 'Telegram' }).check();
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByText('Объявление создано')).toBeVisible();
  expect(api.writes()).toMatchObject([{ method: 'POST', path: '/announcements', body: {
    title: 'Субботнее чтение', body: 'Читаем Евангелие от Иоанна', kind: 'weekly', timezone: 'Europe/Moscow',
    asset_id: null, audience: { platforms: ['telegram'] }, max_runs: null, start_at: null,
    send_time: '10:00:00', days_of_week: [6, 5] } }]);
  await expect(page.getByPlaceholder('Текст')).toHaveValue('');
});

test('объявление: разовая отправка с ограничением по платформам', async ({ page, api }) => {
  await openSection(page, 'Объявления');
  await page.getByPlaceholder('Текст').fill('Обновление бота');
  await page.getByLabel('Как часто').selectOption('once');
  await page.getByRole('checkbox', { name: 'MAX' }).check();
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByText('Объявление создано')).toBeVisible();
  const [request] = api.writes();
  expect(request.body).toEqual({ title: '', body: 'Обновление бота', kind: 'once', timezone: 'Europe/Moscow', asset_id: null,
    audience: { platforms: ['max'] }, max_runs: null, start_at: null });
});

test('объявление: предпросмотр показывает получателей', async ({ page, api }) => {
  await openSection(page, 'Объявления');
  await page.getByPlaceholder('Текст').fill('Мир вам');
  await page.getByRole('button', { name: 'Предпросмотр' }).click();
  await expect(page.getByText('Получателей: 2 249')).toBeVisible();
  expect(api.writes()).toMatchObject([{ method: 'POST', path: '/announcements/preview',
    body: { title: '', body: 'Мир вам', audience: { platforms: [] }, asset_id: null } }]);
});

test('объявление: пауза, правка и отправка сейчас только после подтверждения', async ({ page, api }) => {
  await openSection(page, 'Объявления');
  const row = page.getByRole('row').filter({ hasText: 'Субботнее чтение' });
  await row.getByRole('button', { name: 'Пауза' }).click();
  await expect.poll(() => api.writes().length).toBe(1);

  await row.getByRole('button', { name: 'Сейчас' }).click();

  await answerDialog(page, { accept: false });
  await row.getByRole('button', { name: 'Править' }).click();
  await page.locator('textarea').nth(1).fill('Новый текст');
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  await expect.poll(() => api.writes().length).toBe(2);

  await row.getByRole('button', { name: 'Сейчас' }).click();

  await answerDialog(page);
  await expect(page.getByText('Отправлено')).toBeVisible();
  expect(api.writes()).toMatchObject([
    { method: 'POST', path: '/announcements/1/status', body: { status: 'paused' } },
    { method: 'PATCH', path: '/announcements/1', body: { title: 'Субботнее чтение', body: 'Новый текст', audience: { platforms: ['telegram'] } } },
    { method: 'POST', path: '/announcements/1/run' },
  ]);
});

test('источник: смена режима и включение', async ({ page, api }) => {
  await openSection(page, 'Источники');
  const row = page.getByRole('row').filter({ hasText: 'Община' });
  await row.getByRole('combobox').selectOption('auto');
  await expect.poll(() => api.writes().length).toBe(1);
  await expect(row.getByRole('switch')).toHaveAttribute('aria-checked', 'false');
  await row.getByRole('switch').click();
  await expect.poll(() => api.writes().length).toBe(2);
  expect(api.writes()).toMatchObject([
    { method: 'PATCH', path: '/sources/2', body: { mode: 'auto' } },
    { method: 'PATCH', path: '/sources/2', body: { is_enabled: true } },
  ]);
});

test('источник: публикация поста после подтверждения', async ({ page, api }) => {
  await openSection(page, 'Источники');
  await page.getByRole('button', { name: 'Отправить всем' }).click();
  await answerDialog(page);
  await expect(page.getByText('Поставлено в очередь')).toBeVisible();
  expect(api.writes()).toMatchObject([{ method: 'POST', path: '/posts/7/publish' }]);
});

test('видео: добавление канала', async ({ page, api }) => {
  await openSection(page, 'Видео');
  const form = page.locator('form');
  await form.getByRole('combobox').selectOption('rutube');
  await page.getByPlaceholder('ID канала или ссылка').fill('12345');
  await page.getByRole('checkbox', { name: 'присылать ссылки' }).check();
  await page.getByRole('button', { name: 'Добавить' }).click();
  await expect(page.getByText('Канал добавлен')).toBeVisible();
  expect(api.writes()).toMatchObject([{ method: 'POST', path: '/videos', body: { platform: 'rutube', channel: '12345', notify: true } }]);
});

test('генератор: добавление по шаблону и проверка', async ({ page, api }) => {
  await openSection(page, 'Генераторы картинок');
  await page.getByRole('combobox').filter({ hasText: 'выберите шаблон' }).selectOption('cloudflare-flux');
  await page.getByPlaceholder('Модель').fill('@cf/black-forest-labs/flux-1-schnell');
  await page.getByPlaceholder('Cloudflare Account ID (32 символа)').fill('0123456789abcdef0123456789abcdef');
  await page.getByPlaceholder('Ключ API').fill('secret-token');
  await page.getByRole('button', { name: 'Добавить' }).click();
  await expect(page.getByText('Генератор добавлен')).toBeVisible();
  expect(api.writes()).toMatchObject([{ method: 'POST', path: '/image-providers', body: {
    slug: 'cloudflare-flux', label: 'Cloudflare Workers AI (FLUX)', adapter: 'cloudflare', base_url: '',
    model: '@cf/black-forest-labs/flux-1-schnell', api_key: 'secret-token', account_id: '0123456789abcdef0123456789abcdef',
    width: 1216, height: 832, steps: null, priority: 500, daily_limit: 20, is_enabled: true,
    billing_mode: 'unknown', reservation_usd: '0' } }]);

  await page.getByRole('button', { name: 'Проверить' }).first().click();
  await expect(page.getByRole('img', { name: 'Проверочная картинка' })).toBeVisible();
  expect(api.writes().at(-1)).toMatchObject({ method: 'POST', path: '/image-providers/1/test' });
});

test('картинки: закрепление и карантин', async ({ page, api }) => {
  await openSection(page, 'Картинки');
  await page.getByRole('button', { name: 'Закрепить' }).first().click();
  await expect.poll(() => api.writes().length).toBe(1);
  await page.getByRole('button', { name: 'В карантин' }).first().click();
  await expect.poll(() => api.writes().length).toBe(2);
  expect(api.writes()).toMatchObject([
    { method: 'POST', path: '/illustrations/2/pin?pinned=true' },
    { method: 'POST', path: '/illustrations/1/quarantine?quarantined=true' },
  ]);
});

test('чаты и пользователи: отключение чата и блокировка пользователя', async ({ page, api }) => {
  await openSection(page, 'Чаты и пользователи');
  await page.getByRole('switch', { name: 'Рассылки в чат «Домашняя группа»' }).click();
  await expect.poll(() => api.writes().length).toBe(1);
  await page.getByRole('tab', { name: 'Пользователи' }).click();
  await page.getByRole('switch', { name: 'Доступ к боту: Пётр' }).click();
  await expect.poll(() => api.writes().length).toBe(2);
  expect(api.writes()).toMatchObject([
    { method: 'PUT', path: '/chats/-100500/active', body: { value: false } },
    { method: 'PUT', path: '/users/2/blocked', body: { value: false } },
  ]);
});

test('чаты: поиск и фильтр уходят в запрос', async ({ page, api }) => {
  await openSection(page, 'Чаты и пользователи');
  await page.getByLabel('Платформа').selectOption('max');
  await page.getByPlaceholder('Поиск по названию').fill('дом');
  await expect.poll(() => api.requests.some((r) => r.path === '/chats?platform=max&q=%D0%B4%D0%BE%D0%BC&limit=51&offset=0')).toBe(true);
});

test('доставки: решение только после подтверждения', async ({ page, api }) => {
  await openSection(page, 'Доставки');
  await page.getByRole('button', { name: 'Повторить' }).click();
  await answerDialog(page, { accept: false });
  await page.getByRole('button', { name: 'Дошло' }).click();
  await answerDialog(page);
  await expect.poll(() => api.writes().length).toBe(1);
  expect(api.writes()).toMatchObject([{ method: 'POST', path: '/deliveries/501/resolve', body: { action: 'sent' } }]);
});

test('отчёты: смена периода', async ({ page, api }) => {
  await openSection(page, 'Отчёты');
  await page.getByRole('combobox').selectOption('30');
  await expect.poll(() => api.requests.some((r) => r.path === '/delivery-report?days=30')).toBe(true);
});

test('администраторы: приглашение, смена роли и снятие прав', async ({ page, api }) => {
  await openSection(page, 'Администраторы');
  await page.getByLabel('Роль нового администратора').selectOption('editor');
  await page.getByRole('button', { name: 'Создать приглашение' }).click();
  await expect(page.getByText('/claim ABCD-1234')).toBeVisible();
  await page.getByLabel('Роль: Мария').selectOption('viewer');
  await expect.poll(() => api.writes().length).toBe(2);
  await page.getByRole('button', { name: 'Снять' }).click();
  await answerDialog(page);
  await expect.poll(() => api.writes().length).toBe(3);
  expect(api.writes()).toMatchObject([
    { method: 'POST', path: '/admins/invite?role=editor' },
    { method: 'PUT', path: '/admins/2/role', body: { role: 'viewer' } },
    { method: 'DELETE', path: '/admins/2' },
  ]);
});

test('сеть: добавление прокси по полям и проверка', async ({ page, api }) => {
  await openSection(page, 'Сеть и прокси');
  await page.getByPlaceholder('Название (например, NL-1)').fill('DE-1');
  await page.getByPlaceholder('Хост').fill('de1.example.net');
  await page.getByPlaceholder('Порт').fill('1080');
  await page.getByPlaceholder('Логин (необязательно)').fill('user');
  await page.getByPlaceholder('Пароль', { exact: true }).fill('pass');
  await page.getByRole('button', { name: 'Добавить' }).click();
  await expect(page.getByText('Прокси добавлен')).toBeVisible();
  await page.getByRole('row').filter({ hasText: 'NL-1' }).getByRole('button', { name: 'Проверить' }).click();
  await expect(page.getByText('работает (140 мс, HTTP 200)')).toBeVisible();
  expect(api.writes()).toMatchObject([
    { method: 'POST', path: '/net/proxies', body: { name: 'DE-1', scheme: 'http', host: 'de1.example.net', port: 1080, user: 'user', password: 'pass' } },
    { method: 'POST', path: '/net/proxies/1/test', body: { service: 'telegram' } },
  ]);
});

test('сеть: порядок маршрутов сохраняется', async ({ page, api }) => {
  await openSection(page, 'Сеть и прокси');
  const card = page.getByRole('region', { name: 'Telegram' });
  await card.getByRole('button', { name: 'Переместить «NL-1» ниже' }).click();
  await card.getByRole('button', { name: 'Сохранить' }).click();
  await expect.poll(() => api.writes().length).toBe(1);
  expect(api.writes()).toMatchObject([{ method: 'PUT', path: '/net/services/telegram',
    body: { strategy: 'failover', attempts: 2, routes: [{ proxy_id: null, enabled: true }, { proxy_id: 1, enabled: true }] } }]);
});

test('резервные копии: скачивание файла', async ({ page, api }) => {
  await openSection(page, 'Резервные копии');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Скачать .dump biblebot-2026-10-16.dump.age' }).click();
  expect((await download).suggestedFilename()).toBe('biblebot-2026-10-16.dump');
  expect(api.writes()).toMatchObject([{ method: 'POST', path: '/backups/2/download' }]);
});

test('настройки: скрытие раздела поддержки', async ({ page, api }) => {
  await openSection(page, 'Настройки');
  await page.getByRole('switch', { name: 'Раздел «Поддержка проекта» показан' }).click();
  await expect.poll(() => api.writes().length).toBe(1);
  expect(api.writes()).toMatchObject([{ method: 'PUT', path: '/settings', body: { support_section: false } }]);
});

test('состояние: «Обновить» перечитывает сводку и версию, «Требуют внимания» ведёт в доставки', async ({ page, api }) => {
  await api.settle();
  const count = (path) => api.requests.filter((r) => r.path === path).length;
  const before = [count('/overview'), count('/release')];
  await page.getByRole('button', { name: 'Обновить' }).click();
  await expect.poll(() => [count('/overview'), count('/release')]).toEqual([before[0] + 1, before[1] + 1]);
  await page.getByRole('link', { name: /Требуют внимания/ }).click();
  await expect(page).toHaveURL(/#\/deliveries$/);
});

test('доставки: пустой список показывает «Всё в порядке»', async ({ page, api }) => {
  api.on('GET', /^\/deliveries\/attention\?/, []);
  await openSection(page, 'Доставки');
  await expect(page.getByText('Всё в порядке')).toBeVisible();
  await expect(page.getByRole('table')).toHaveCount(0);
});

test('объявление: картинка загружается и уходит в asset_id, импорт JSON отправляет список', async ({ page, api }) => {
  await openSection(page, 'Объявления');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');
  await page.getByLabel('Выбрать картинку').setInputFiles({ name: 'cover.png', mimeType: 'image/png', buffer: png });
  await expect(page.getByText('Загружена #42')).toBeVisible();
  await page.getByPlaceholder('Текст').fill('С картинкой');
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByText('Объявление создано')).toBeVisible();

  const items = [{ body: 'Из файла', kind: 'once' }];
  await page.getByLabel('Импорт JSON').setInputFiles({ name: 'plan.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(items)) });
  await expect(page.getByText('Импортировано')).toBeVisible();
  const writes = api.writes();
  expect(writes.map((w) => `${w.method} ${w.path}`)).toEqual(['POST /media', 'POST /announcements', 'POST /announcements/import']);
  expect(writes[1].body).toMatchObject({ body: 'С картинкой', asset_id: 42 });
  expect(writes[2].body).toEqual(items);
});

test('видео: переключатели проверки и рассылки, интервал, удаление после подтверждения', async ({ page, api }) => {
  await openSection(page, 'Видео');
  const row = page.getByRole('row').filter({ hasText: 'Библейские уроки' });
  await row.getByRole('switch', { name: /Присылать ссылки/ }).click();
  await expect.poll(() => api.writes().length).toBe(1);
  await row.getByRole('switch', { name: /Проверять канал/ }).click();
  await expect.poll(() => api.writes().length).toBe(2);
  await row.getByRole('combobox').selectOption('180');
  await expect.poll(() => api.writes().length).toBe(3);
  await row.getByRole('button', { name: 'Удалить' }).click();
  await answerDialog(page, { accept: false });
  await row.getByRole('button', { name: 'Удалить' }).click();
  await answerDialog(page);
  await expect.poll(() => api.writes().length).toBe(4);
  expect(api.writes()).toMatchObject([
    { method: 'PATCH', path: '/videos/1', body: { notify: false } },
    { method: 'PATCH', path: '/videos/1', body: { is_enabled: false } },
    { method: 'PATCH', path: '/videos/1', body: { interval_minutes: 180 } },
    { method: 'DELETE', path: '/videos/1' },
  ]);
});

test('генератор: выключение переключателем и правка настроек', async ({ page, api }) => {
  await openSection(page, 'Генераторы картинок');
  await page.getByRole('switch', { name: /Cloudflare Flux/ }).click();
  await expect.poll(() => api.writes().length).toBe(1);
  await page.getByRole('button', { name: 'Настроить' }).click();
  await page.getByLabel('В сутки').fill('50');
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  await expect.poll(() => api.writes().length).toBe(2);
  expect(api.writes()).toMatchObject([
    { method: 'PATCH', path: '/image-providers/1', body: { is_enabled: false } },
    { method: 'PATCH', path: '/image-providers/1', body: { daily_limit: 50 } },
  ]);
});

test('окна панели: шаблон сохраняется через окно ввода, Esc отменяет подтверждение', async ({ page, api }) => {
  await openSection(page, 'Объявления');
  await page.getByPlaceholder('Заголовок (необязательно)').fill('Суббота');
  await page.getByPlaceholder('Текст').fill('Мир вам');
  await page.getByRole('button', { name: 'Сохранить как шаблон' }).click();
  await expect(page.getByRole('dialog').getByRole('textbox')).toHaveValue('Суббота');
  await answerDialog(page, { text: 'Субботнее приветствие 2' });
  await expect(page.getByText('Шаблон сохранён')).toBeVisible();

  await page.getByRole('row').filter({ hasText: 'Субботнее чтение' }).getByRole('button', { name: 'Сейчас' }).click();
  await expect(page.getByRole('dialog')).toContainText('Отправить «Субботнее чтение» сейчас?');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toBeHidden();
  expect(api.writes()).toMatchObject([{ method: 'POST', path: '/announcement-templates', body: { name: 'Субботнее приветствие 2', title: 'Суббота', body: 'Мир вам' } }]);
});

test('ошибка действия показывается рядом с формой, успех — уведомлением', async ({ page, api }) => {
  api.on('POST', '/videos', { status: 422, json: { detail: 'invalid' } });
  await openSection(page, 'Видео');
  await page.getByPlaceholder('ID канала или ссылка').fill('плохой');
  await page.getByRole('button', { name: 'Добавить' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'Проверьте введённые данные' })).toBeVisible();
  api.problems = api.problems.filter((p) => !p.includes('status of 422'));
  await page.getByRole('switch', { name: /Присылать ссылки/ }).first().click();
  await expect(page.locator('.toast')).toContainText('Сохранено');
});

test('ключ перестал подходить посреди работы — возврат на экран входа с пояснением', async ({ page, api }) => {
  await openSection(page, 'Журнал');
  // Сначала дождаться обычной загрузки журнала, иначе 401 получит она, а не нажатие «Обновить».
  await expect(page.getByRole('table').getByText('announcement.create')).toBeVisible();
  await api.settle();
  api.on('GET', /^\/audit\?/, { status: 401, json: { detail: 'unauthorized' } });
  await page.getByRole('button', { name: 'Обновить' }).click();
  await expect(page.getByText('Ключ больше не подходит')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
});

test('коды ошибок API показываются понятным текстом', async ({ page, api }) => {
  api.on('POST', '/net/proxies', { status: 409, json: { detail: 'name_taken' } });
  api.on('POST', '/announcements/import', { status: 422, json: { detail: 'item_2:invalid_days' } });
  await openSection(page, 'Сеть и прокси');
  await page.getByPlaceholder('Название (например, NL-1)').fill('NL-1');
  await page.getByPlaceholder('Хост').fill('nl1.example.net');
  await page.getByPlaceholder('Порт').fill('1080');
  await page.getByRole('button', { name: 'Добавить' }).click();
  await expect(page.getByText('⚠ Прокси с таким названием уже есть — выберите другое')).toBeVisible();

  await openSection(page, 'Объявления');
  const items = [{ body: 'Первое', kind: 'once' }, { body: 'Второе', kind: 'weekly' }];
  await page.getByLabel('Импорт JSON').setInputFiles({ name: 'plan.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(items)) });
  await expect(page.getByText('Объявление № 2: Неверные дни недели')).toBeVisible();
  api.problems = api.problems.filter((p) => !/status of (409|422)/.test(p));
});

test('журнал: фильтры и страницы уходят в запрос', async ({ page, api }) => {
  const row = (n) => ({ id: n, created_at: '2026-10-16T09:00:00+00:00', actor: 'panel', source: 'panel', action: 'source.enable', target: `Источник ${n}`, details: {} });
  api.on('GET', /^\/audit\?.*offset=0&/, { has_more: true, items: Array.from({ length: 50 }, (_, i) => row(100 - i)) });
  api.on('GET', /^\/audit\?.*offset=50&/, { has_more: false, items: [row(50), row(49)] });
  await openSection(page, 'Журнал');
  await expect(page.getByText('Записи 1–50')).toBeVisible();
  await page.getByRole('button', { name: 'Старее' }).click();
  await expect(page.getByText('Записи 51–52')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Старее' })).toBeDisabled();

  // Любой фильтр возвращает на первую страницу.
  await page.getByLabel('Откуда').selectOption('bot');
  await expect(page.getByText('Записи 1–50')).toBeVisible();
  await page.getByLabel('Действие').selectOption('source.enable');
  await page.getByLabel('Период').selectOption('7');
  await page.getByLabel('Поиск в журнале').fill('Община');
  await api.settle();
  const audit = api.requests.filter((r) => r.path.startsWith('/audit?')).map((r) => Object.fromEntries(new URLSearchParams(r.path.split('?')[1])));
  expect(audit[0]).toEqual({ limit: '50', offset: '0', q: '', source: '', action: '', days: '0' });
  expect(audit[1]).toMatchObject({ offset: '50' });
  expect(audit.at(-1)).toEqual({ limit: '50', offset: '0', q: 'Община', source: 'bot', action: 'source.enable', days: '7' });
  // Поиск с задержкой: промежуточные буквы не порождают запросов.
  expect(audit.filter((r) => r.q && r.q !== 'Община')).toEqual([]);
});

test('длинные списки листаются страницами; поиск возвращает на первую', async ({ page, api }) => {
  const params = (path) => Object.fromEntries(new URLSearchParams(path.split('?')[1]));
  const page1 = (make) => Array.from({ length: 51 }, (_, i) => make(i + 1));
  const chat = (n) => ({ id: n, platform: 'telegram', chat_type: 'group', title: `Группа ${n}`, username: '', ui_language: 'ru', timezone: 'UTC', is_active: true, updated_at: null });
  const user = (n) => ({ id: n, platform: 'telegram', username: '', first_name: `Имя ${n}`, is_owner: false, is_admin: false, is_blocked: false });
  api.on('GET', /^\/chats\?/, ({ path }) => (params(path).offset === '0' ? page1(chat) : [chat(51), chat(52)]));
  api.on('GET', /^\/users\?/, ({ path }) => (params(path).offset === '0' ? page1(user) : [user(51)]));

  await openSection(page, 'Чаты и пользователи');
  const pager = page.getByRole('navigation', { name: 'Страницы чатов' });
  await expect(pager.getByText('Чаты 1–50')).toBeVisible();
  await expect(page.getByText('Группа 51', { exact: true })).toHaveCount(0);
  await pager.getByRole('button', { name: 'Дальше' }).click();
  await expect(pager.getByText('Чаты 51–52')).toBeVisible();
  await expect(page.getByText('Группа 52', { exact: true })).toBeVisible();
  await page.getByPlaceholder('Поиск по названию').fill('Группа');
  await expect(pager.getByText('Чаты 1–50')).toBeVisible();

  await page.getByRole('tab', { name: /Пользователи/ }).click();
  const users = page.getByRole('navigation', { name: 'Страницы пользователей' });
  await users.getByRole('button', { name: 'Дальше' }).click();
  await expect(users.getByText('Пользователи 51–51')).toBeVisible();
  await expect(users.getByRole('button', { name: 'Дальше' })).toBeDisabled();
  await api.settle();

  const asked = (prefix) => api.requests.filter((r) => r.path.startsWith(prefix)).map((r) => params(r.path));
  expect(asked('/chats?')[0]).toEqual({ platform: '', q: '', limit: '51', offset: '0' });
  expect(asked('/chats?').map((p) => p.offset)).toContain('50');
  expect(asked('/chats?').at(-1)).toEqual({ platform: '', q: 'Группа', limit: '51', offset: '0' });
  expect(asked('/users?').map((p) => p.offset)).toEqual(['0', '50']);
});

test('доставки и картинки: кнопки страниц появляются, только когда есть следующая', async ({ page, api }) => {
  await openSection(page, 'Доставки');
  await expect(page.getByRole('navigation', { name: 'Страницы доставок' })).toHaveCount(0);
  await openSection(page, 'Картинки');
  await expect(page.getByRole('navigation', { name: 'Страницы картинок' })).toHaveCount(0);
  await api.settle();
  expect(api.requests.some((r) => r.path === '/deliveries/attention?limit=51&offset=0')).toBe(true);
  expect(api.requests.some((r) => r.path === '/illustrations?q=&limit=25&offset=0')).toBe(true);
});

test('объявления, очередь постов и резервные копии листаются страницами', async ({ page, api }) => {
  const params = (path) => Object.fromEntries(new URLSearchParams(path.split('?')[1]));
  const many = (count, make, from = 1) => Array.from({ length: count }, (_, i) => make(from + i));
  const note = (n) => ({ ...api.data['/announcements'][1], id: 1000 - n, title: `Объявление ${n}` });
  const post = (n) => ({ ...api.data['/posts'][0], id: 1000 - n, body: `Пост ${n}` });
  api.on('GET', /^\/announcements\?/, ({ path }) => (params(path).offset === '0' ? many(51, note) : many(3, note, 51)));
  api.on('GET', /^\/posts\?/, ({ path }) => (params(path).offset === '0' ? many(21, post) : many(1, post, 21)));
  const copy = (n) => ({ id: 100 - n, filename: `biblebot-${n}.dump.age`, created_at: null, verified_at: null, size_bytes: 1024 });
  api.on('GET', /^\/backups\?/, ({ path }) => {
    const offset = Number(params(path).offset);
    return { total: 35, limit: 30, offset, download_enabled: false, items: many(Math.min(30, 35 - offset), copy, offset + 1) };
  });

  await openSection(page, 'Объявления');
  const notes = page.getByRole('navigation', { name: 'Страницы объявлений' });
  await expect(notes.getByText('Объявления 1–50')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Объявления', exact: true, level: 2 })).toBeVisible();   // без числа: оно неполное
  await notes.getByRole('button', { name: 'Старее' }).click();
  await expect(notes.getByText('Объявления 51–53')).toBeVisible();
  await expect(page.getByText('Объявление 53', { exact: true })).toBeVisible();

  await openSection(page, 'Источники');
  const posts = page.getByRole('navigation', { name: 'Страницы постов' });
  await posts.getByRole('button', { name: 'Старее' }).click();
  await expect(posts.getByText('Посты 21–21')).toBeVisible();
  await expect(page.getByText('Пост 21', { exact: true })).toBeVisible();

  await openSection(page, 'Резервные копии');
  const copies = page.getByRole('navigation', { name: 'Страницы копий' });
  await copies.getByRole('button', { name: 'Старее' }).click();
  await expect(copies.getByText('Копии 31–35')).toBeVisible();
  await expect(copies.getByRole('button', { name: 'Старее' })).toBeDisabled();
  await api.settle();

  const asked = (prefix) => api.requests.filter((r) => r.path.startsWith(prefix)).map((r) => params(r.path));
  expect(asked('/announcements?').map((p) => [p.limit, p.offset])).toEqual([['51', '0'], ['51', '50']]);
  expect(asked('/posts?').map((p) => [p.status, p.limit, p.offset])).toEqual([['new', '21', '0'], ['new', '21', '20']]);
  expect(asked('/backups?').map((p) => p.offset)).toEqual(['0', '30']);
});

test('одна страница — без кнопок и с числом записей в заголовке', async ({ page }) => {
  await openSection(page, 'Объявления');
  await expect(page.getByRole('heading', { name: 'Объявления (3)' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Страницы объявлений' })).toHaveCount(0);
  await openSection(page, 'Резервные копии');
  await expect(page.getByRole('heading', { name: 'Копии (2)' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Страницы копий' })).toHaveCount(0);
});
