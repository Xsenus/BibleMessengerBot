// Ответы /api/admin для тестов. Форма полей повторяет сервер (app/web/api*.py, sql/schema.sql):
// при изменении API обновляйте моки вместе с панелью.
export const BASE = Date.parse('2026-10-16T08:00:00Z');
const ago = (minutes) => new Date(BASE - minutes * 60000).toISOString();
const audience = (patch = {}) => ({ platforms: [], chat_types: [], chat_ids: [], ...patch });

const announcement = (patch) => ({
  id: 0, title: '', body: '', asset_id: null, audience: audience(), created_by: 1, status: 'active', kind: 'once',
  send_time: null, timezone: 'Europe/Moscow', days_of_week: [1, 2, 3, 4, 5, 6, 7], day_of_month: null, interval_minutes: null,
  start_at: null, end_at: null, max_runs: null, runs_done: 0, next_run_at: null, misfire_grace_minutes: 60,
  created_at: ago(20000), updated_at: ago(100), ...patch,
});

const provider = {
  id: 1, slug: 'cf-flux', label: 'Cloudflare Flux', adapter: 'cloudflare', base_url: '', model: '@cf/black-forest-labs/flux-1-schnell',
  account_id: '0123456789abcdef0123456789abcdef', width: 1216, height: 832, steps: null, is_enabled: true, priority: 100, daily_limit: 20,
  extra: {}, last_test_at: ago(600), last_test_ok: true, last_test_error: '', created_at: ago(9000), updated_at: ago(600),
  billing_mode: 'metered', reservation_usd: '0.0200', has_key: true, needs_key: true, pricing_ready: true, daily_used: 7, host: 'api.cloudflare.com',
};

const attempt = (patch) => ({
  commit: 'c04cdef1a2b3c4d5e6f708192a3b4c5d6e7f8091', previous_commit: 'c9b5116aa', changes: null, status: 'succeeded', stage: 'completed',
  started_at: ago(310), finished_at: ago(302), updated_at: ago(302), failure: null, legacy: false, commit_url: 'https://github.com/Xsenus/BibleMessengerBot/commit/c04cdef',
  ...patch,
});

/** Ответы на GET: ключ — путь после /api/admin с query или без него. */
export const readResponses = {
  '/overview': {
    platforms: [{ platform: 'telegram', count: 1840 }, { platform: 'max', count: 312 }, { platform: 'vk', count: 97 }],
    queue: { queued: 12, attention: 3, sent_24h: 4821 },
    services: ['bot', 'composer', 'illustrator', 'maxbot', 'speaker', 'vkbot', 'worker'].map((service) => ({ service, last_seen: ago(service === 'illustrator' ? 30 : 1) })),
  },
  '/release': {
    build: { version: '1.15.0', commit: 'c04cdef1a2b3c4d5e6f708192a3b4c5d6e7f8091', built_at: ago(320), commit_url: 'https://github.com/Xsenus/BibleMessengerBot/commit/c04cdef' },
    server: {
      available: true, stale: false,
      current: { ...attempt({ changes: { state: 'ready', source: 'changelog', items: ['Описания изменений в истории деплоев'], more: false, reason: null, compare_url: 'https://github.com/' } }), services: [] },
      last_attempt: { ...attempt({}), services: [{ name: 'bot', health: 'healthy', commit: 'c04cdef' }] },
      history: [
        { ...attempt({ changes: { state: 'ready', source: 'changelog', items: ['Описания изменений в истории деплоев', 'Стили таблицы истории'], more: false, reason: null, compare_url: 'https://github.com/' } }), id: 'a1b2c3d4e5f6a7b8c9d0e1f2', stale: false },
        { ...attempt({ status: 'rolled_back', stage: 'rollback', commit: 'c9b5116aa', started_at: ago(1500), finished_at: ago(1490),
          failure: { code: 'healthcheck_failed', message: 'Служба bot не прошла проверку после обновления', stage: 'verifying', exit_code: 1, service: 'bot' } }), id: 'b1b2c3d4e5f6a7b8c9d0e1f2', stale: false },
      ],
    },
    github: {
      available: true, checked_at: ago(1), stale: false, history_complete: true,
      run: { id: 11, commit: 'c04cdef1a2b3c4d5e6f708192a3b4c5d6e7f8091', status: 'completed', conclusion: 'success', started_at: ago(312), updated_at: ago(300), attempt: 1, url: 'https://github.com/', attempt_url: 'https://github.com/' },
      history: [{ id: 11, commit: 'c04cdef1a2b3c4d5e6f708192a3b4c5d6e7f8091', status: 'completed', conclusion: 'success', started_at: ago(312), updated_at: ago(300), attempt: 1, url: 'https://github.com/', attempt_url: 'https://github.com/' }],
    },
    bots: [
      { service: 'bot', label: 'Telegram', expected: true, verified: true, live: true, last_seen: ago(1) },
      { service: 'maxbot', label: 'MAX', expected: true, verified: true, live: true, last_seen: ago(1) },
      { service: 'vkbot', label: 'ВКонтакте', expected: false, verified: false, live: null, last_seen: null },
    ],
  },
  '/announcements': [
    announcement({ id: 1, title: 'Субботнее чтение', body: 'Друзья, напоминаем: в субботу в 10:00 совместное чтение Евангелия от Иоанна.', kind: 'weekly', send_time: '10:00:00', days_of_week: [6], runs_done: 14, next_run_at: ago(-2000), audience: audience({ platforms: ['telegram'] }) }),
    announcement({ id: 2, body: 'Обновление бота: добавлены озвучки глав.', status: 'finished', runs_done: 1, max_runs: 1 }),
    announcement({ id: 3, title: 'Пост', body: 'Ежедневное напоминание о молитве.', kind: 'daily', send_time: '07:30:00', status: 'paused', runs_done: 40, max_runs: 100, audience: audience({ platforms: ['max', 'vk'], chat_types: ['private'] }) }),
  ],
  '/announcement-templates': [{ id: 1, name: 'Субботнее приветствие', title: 'Добрая суббота', body: 'Мир вам!', created_at: ago(9000) }],
  '/sources': [
    { id: 1, platform: 'telegram', external_id: -1001234567890, title: 'Слово на каждый день', chat_id: null, mode: 'approve', audience: audience(), prefix: '', min_length: 40, is_enabled: true, created_at: ago(9000) },
    { id: 2, platform: 'vk', external_id: 123456, title: 'Община', chat_id: null, mode: 'capture', audience: audience({ platforms: ['vk'] }), prefix: '', min_length: 1, is_enabled: false, created_at: ago(8000) },
  ],
  '/posts?status=new': [{ id: 7, source_id: 1, body: '«Придите ко Мне, все труждающиеся и обремененные, и Я успокою вас» (Мф 11:28).', status: 'new', posted_at: ago(90), source_title: 'Слово на каждый день', platform: 'telegram' }],
  '/videos': [
    { id: 1, platform: 'youtube', handle: 'UCabcdefghijklmnopqrstuv', title: 'Библейские уроки', is_enabled: true, notify: true, audience: audience(), template: '', interval_minutes: 60, baselined: true, next_check_at: ago(-40), last_checked_at: ago(20), error_count: 0, last_error: '', created_at: ago(9000) },
    { id: 2, platform: 'rutube', handle: '12345', title: '', is_enabled: true, notify: false, audience: audience(), template: '', interval_minutes: 180, baselined: true, next_check_at: ago(-80), last_checked_at: ago(100), error_count: 2, last_error: 'HTTP 503 от rutube.ru', created_at: ago(9000) },
  ],
  '/image-providers': {
    mode: 'auto', monthly_budget_usd: '10',
    builtin: [{ name: 'openai', model: 'gpt-image-1', configured: false, daily_used: 0, daily_limit: 10, blocked_until: null, error_code: null }],
    custom: [provider], jobs: { queued: 4, done: 210, failed: 2 },
    templates: [{ id: 'cloudflare-flux', label: 'Cloudflare Workers AI (FLUX)', adapter: 'cloudflare', model: '@cf/black-forest-labs/flux-1-schnell', needs_key: true, needs_account: true, steps: null, width: 1216, height: 832, signup: 'https://dash.cloudflare.com/', free: 'Бесплатно до 10 000 нейронов в сутки.', howto: ['Зарегистрируйтесь', 'Создайте токен Workers AI'] }],
    secrets_ready: true, secrets_dedicated: false, planner_ready: true,
  },
  '/image-tuning': { ranking: 'auto', style: 'oil painting, warm light', stats: { 'cf-flux': { attempts: 120, ok: 114, rejected: 3, avg_seconds: 6.2, success_rate: 0.95, score: 0.91 } } },
  '/illustrations': { items: [1, 2, 3, 4].map((id) => ({ id, book_code: 'JHN', chapter: 3, verse: 10 + id, verse_end: null, artwork_scope: 'verse', pinned: id === 1, quarantined_at: id === 4 ? ago(10) : null, generated_at: ago(id * 300), translation_id: 1 })) },
  '/chats': [
    { id: -100500, platform: 'telegram', chat_type: 'group', title: 'Домашняя группа', username: '', ui_language: 'ru', timezone: 'Europe/Moscow', is_active: true, updated_at: ago(30) },
    { id: 777, platform: 'max', chat_type: 'private', title: '', username: 'ivan', ui_language: 'ru', timezone: 'Asia/Yekaterinburg', is_active: false, updated_at: ago(400) },
  ],
  '/users': [
    { id: 1, platform: 'telegram', username: 'anna', first_name: 'Анна', is_owner: true, is_admin: true, is_blocked: false },
    { id: 2, platform: 'vk', username: '', first_name: 'Пётр', is_owner: false, is_admin: false, is_blocked: true },
  ],
  '/deliveries/attention': [{ id: 501, chat_id: -100500, mode: 'verse_of_day', status: 'uncertain', error_code: 'timeout', attempt_count: 2, updated_at: ago(45) }],
  '/delivery-report': {
    per_day: [{ day: '2026-10-15', mode: 'verse_of_day', total: 1800, sent: 1795, failed: 5 }, { day: '2026-10-14', mode: 'verse_of_day', total: 1790, sent: 1790, failed: 0 }],
    errors: [{ error_code: 'chat_not_found', mode: 'verse_of_day', n: 4 }],
    runs: [{ id: 9, announcement_id: 1, title: 'Субботнее чтение', created_at: ago(5000), status: 'done', recipients: 1840, queued: 0, skipped: 0, sent: 1838, failed: 2 }],
  },
  '/audit': { has_more: false, items: [
    { id: 2, created_at: ago(5), actor: 'panel', source: 'panel', action: 'announcement.create', target: '#1', details: {} },
    { id: 1, created_at: ago(60), actor: '2', source: 'bot', action: 'source.enable', target: 'Община', details: {} },
  ] },
  '/audit/actions': ['announcement.create', 'source.enable'],
  '/admins': [
    { telegram_user_id: 1, platform: 'telegram', username: 'anna', first_name: 'Анна', is_owner: true, admin_role: 'admin' },
    { telegram_user_id: 2, platform: 'max', username: null, first_name: 'Мария', is_owner: false, admin_role: 'editor' },
  ],
  '/net': {
    services: [
      { key: 'telegram', label: 'Telegram', target: 'https://api.telegram.org', strategy: 'failover', attempts: 2, configured: true, routes: [
        { proxy_id: 1, name: 'NL-1', enabled: true, health: { successes: 120, failures: 1, latency_ms: 140.4, open_seconds: 0, last_error: '' } },
        { proxy_id: null, name: 'Напрямую', enabled: true, health: null }] },
      { key: 'youtube', label: 'YouTube', target: 'https://www.youtube.com', strategy: 'failover', attempts: 3, configured: false, routes: [] },
    ],
    proxies: [{ id: 1, name: 'NL-1', scheme: 'socks5', host: 'nl1.example.net', is_enabled: true, last_test_ok: true, last_test_error: '', last_test_at: ago(50) }],
    strategies: ['failover', 'auto', 'round_robin'], secrets_ready: true,
  },
  '/backups': { total: 2, limit: 30, offset: 0, download_enabled: true, items: [
    { id: 2, filename: 'biblebot-2026-10-16.dump.age', created_at: ago(600), verified_at: ago(590), size_bytes: 48_500_000 },
    { id: 1, filename: 'biblebot-2026-10-15.dump.age', created_at: ago(2000), verified_at: ago(1990), size_bytes: 48_100_000 },
  ] },
  '/backups/1/availability': { id: 1, status: 'missing' },
  '/backups/2/availability': { id: 2, status: 'available' },
  '/settings': { support_section: true, support_source: 'panel' },
};

/** Ответы на изменяющие запросы. */
export const writeResponses = [
  { method: 'POST', path: /^\/announcements$/, reply: announcement({ id: 4, body: 'создано' }) },
  { method: 'POST', path: /^\/announcements\/import$/, reply: { created: [5, 6] } },
  { method: 'POST', path: /^\/announcements\/preview$/, reply: { total: 2249, platforms: {
    telegram: { recipients: 1840, parts: ['Мир вам'] }, max: { recipients: 312, parts: ['Мир вам'] }, vk: { recipients: 97, parts: ['Мир вам'] } } } },
  { method: 'POST', path: /^\/announcements\/\d+\/(run|status)$/, reply: { ok: true } },
  { method: 'PATCH', path: /^\/announcements\/\d+$/, reply: { ok: true } },
  { method: 'POST', path: /^\/announcement-templates$/, reply: { id: 2 } },
  { method: 'POST', path: /^\/media$/, reply: { asset_id: 42 } },
  { method: 'PATCH', path: /^\/sources\/\d+$/, reply: { ok: true } },
  { method: 'POST', path: /^\/posts\/\d+\/(publish|ignore)$/, reply: { ok: true } },
  { method: 'POST', path: /^\/videos$/, reply: { id: 3 } },
  { method: 'PATCH', path: /^\/videos\/\d+$/, reply: { ok: true } },
  { method: 'DELETE', path: /^\/videos\/\d+$/, reply: { ok: true } },
  { method: 'POST', path: /^\/image-providers$/, reply: { ...provider, id: 2 } },
  { method: 'PATCH', path: /^\/image-providers\/\d+$/, reply: { ok: true } },
  { method: 'DELETE', path: /^\/image-providers\/\d+$/, reply: { ok: true } },
  { method: 'POST', path: /^\/image-providers\/\d+\/test$/, reply: { ok: true, error: '', bytes: 70, seconds: 1.2,
    preview: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==' } },
  { method: 'POST', path: /^\/image-queue\/retry$/, reply: { retried: 2 } },
  { method: 'PUT', path: /^\/image-tuning$/, reply: { ok: true } },
  { method: 'POST', path: /^\/illustrations\/\d+\/(pin|variant|quarantine)/, reply: { ok: true } },
  { method: 'PUT', path: /^\/(chats|users)\/-?\d+\/(active|blocked)$/, reply: { ok: true } },
  { method: 'POST', path: /^\/deliveries\/\d+\/resolve$/, reply: { ok: true } },
  { method: 'POST', path: /^\/admins\/invite\?role=\w+$/, reply: { code: 'ABCD-1234', role: 'editor' } },
  { method: 'PUT', path: /^\/admins\/\d+\/role$/, reply: { ok: true } },
  { method: 'DELETE', path: /^\/admins\/\d+$/, reply: { ok: true } },
  { method: 'POST', path: /^\/net\/proxies$/, reply: { id: 2 } },
  { method: 'PATCH', path: /^\/net\/proxies\/\d+$/, reply: { ok: true } },
  { method: 'DELETE', path: /^\/net\/proxies\/\d+$/, reply: { ok: true } },
  { method: 'POST', path: /^\/net\/proxies\/\d+\/test$/, reply: { ok: true, status: 200, ms: 140, error: '' } },
  { method: 'PUT', path: /^\/net\/services\/\w+$/, reply: { ok: true } },
  { method: 'POST', path: /^\/net\/services\/\w+\/probe$/, reply: [
    { route: 'proxy:1', label: 'NL-1', ok: true, status: 200, ms: 140, error: '' },
    { route: 'direct', label: 'Напрямую', ok: false, status: 0, ms: 5000, error: 'ConnectTimeout' }] },
  { method: 'POST', path: /^\/backups\/\d+\/download$/, file: Buffer.from('PGDMP'),
    headers: { 'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment; filename="biblebot-2026-10-16.dump"' } },
  { method: 'PUT', path: /^\/settings$/, reply: { support_section: false, support_source: 'panel' } },
];
