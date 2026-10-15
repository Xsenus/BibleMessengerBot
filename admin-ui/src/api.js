// Тонкий клиент к /api/admin: ключ хранится только в sessionStorage вкладки и уходит заголовком X-Admin-Key.
const KEY = 'biblebot-admin-key';

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
  let response;
  try { response = await fetch(`/api/admin${path}`, { method, headers, body: payload, signal }); }
  catch { throw new ApiError(0, 'Нет связи с сервером'); }
  if (!response.ok) {
    let detail = '';
    try { const data = await response.json(); detail = typeof data.detail === 'string' ? data.detail : 'Некорректные данные'; } catch { /* пустой ответ */ }
    throw new ApiError(response.status, detail);
  }
  return response.json();
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
