// Тонкий клиент к /api/admin: ключ хранится только в sessionStorage вкладки и уходит заголовком X-Admin-Key.
const KEY = 'biblebot-admin-key';
const READ_TIMEOUT_MS = 15000;

export const getKey = () => {
  try { return sessionStorage.getItem(KEY) || ''; } catch { return ''; }
};
export const setKey = (value) => {
  try { value ? sessionStorage.setItem(KEY, value) : sessionStorage.removeItem(KEY); } catch { /* нет хранилища */ }
};

export class ApiError extends Error {
  constructor(status, detail) { super(detail || String(status)); this.status = status; }
}

export async function api(path, { method = 'GET', body, raw, signal } = {}) {
  const headers = { 'X-Admin-Key': getKey() };
  let payload;
  if (raw) payload = raw;
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
  const read = method === 'GET' ? new AbortController() : null;
  const abort = () => read?.abort(signal.reason);
  const timer = read && setTimeout(() => read.abort(new DOMException('Read timeout', 'TimeoutError')), READ_TIMEOUT_MS);
  if (read && signal) {
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort, { once: true });
  }
  try {
    const response = await fetch(`/api/admin${path}`, { method, headers, body: payload, signal: read?.signal ?? signal });
    if (!response.ok) {
      // Ключ перестал подходить (сменили на сервере, истёк сеанс) — панель вернётся на экран входа.
      if (response.status === 401) window.dispatchEvent(new CustomEvent('admin:unauthorized'));
      let detail = [502, 503, 504].includes(response.status) ? 'Сервер временно недоступен. Попробуйте обновить раздел.' : '';
      try {
        const data = await response.json();
        if (typeof data.detail === 'string') detail = data.detail;
      } catch { /* прокси может вернуть HTML вместо JSON */ }
      throw new ApiError(response.status, detail);
    }
    return await response.json();
  } catch (error) {
    if (read?.signal.reason?.name === 'TimeoutError') {
      throw new ApiError(0, 'Сервер не ответил за 15 секунд. Попробуйте обновить раздел.');
    }
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, 'Нет связи с сервером');
  } finally {
    if (read) {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }
}

/** Скачать приватный файл с ключом в заголовке; ключ и S3-ссылки не попадают в URL. */
export async function downloadBackup(id) {
  let response;
  try { response = await fetch(`/api/admin/backups/${id}/download`, { method: 'POST', headers: { 'X-Admin-Key': getKey() } }); }
  catch { throw new ApiError(0, 'Нет связи с сервером'); }
  if (!response.ok) {
    let detail = 'Не удалось скачать резервную копию';
    try { const data = await response.json(); if (typeof data.detail === 'string') detail = data.detail; } catch { /* пустой ответ */ }
    throw new ApiError(response.status, detail);
  }
  let blob;
  try { blob = await response.blob(); }
  catch { throw new ApiError(0, 'Скачивание прервалось. Повторите попытку.'); }
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  const name = response.headers.get('Content-Disposition')?.match(/filename="([A-Za-z0-9._-]+\.dump)"/)?.[1];
  link.href = url; link.download = name || `biblebot-backup-${id}.dump`;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
